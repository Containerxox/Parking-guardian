import os
import cv2
import numpy as np
import subprocess
import json
import sqlite3  # 🚀 SQLite3 임포트
from flask import Flask, request, jsonify,Blueprint,current_app
from werkzeug.utils import secure_filename
#from mms import send_mms_via_aligo
import torch # torch 라이브러리 추가

os.environ['MKL_THREADING_LAYER'] = 'GNU'

#app = Flask(__name__)
model_app = Blueprint('model_app', __name__)

# --- 설정 영역 ---
# 이미지가 임시 저장될 폴더 (OCR용)
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# 🚀 새로 추가: SQLite 데이터베이스 경로
DATABASE = 'database/violations.db'
# YOLOv5 설정
YOLOV5_PATH = '../yolov5'  # 로컬 YOLOv5 폴더 경로
WINDOW_MODEL_PATH = 'models/Detact_Window_model.pt'
STICKER_MODEL_PATH = 'models/Detact_Sticker_model.pt'
PLATE_MODEL_PATH = 'models/Detact_Plate_model.pt'
WINDOW_MODEL=''
STICKER_MODEL=''
PLATE_MODEL=''
# Conda 환경 설정
CONDA_ENV = 'yolov5'

# --- 🚀 최적화 1: 앱 시작 시 모델 미리 로드 ---
# 애플리케이션이 시작될 때 모델을 한 번만 VRAM에 로드합니다.
def init_models():
    print("YOLOv5 모델을 로드합니다...")
    try:
        global WINDOW_MODEL, STICKER_MODEL, PLATE_MODEL
        WINDOW_MODEL = torch.hub.load(YOLOV5_PATH, 'custom', path=WINDOW_MODEL_PATH, source='local')
        STICKER_MODEL = torch.hub.load(YOLOV5_PATH, 'custom', path=STICKER_MODEL_PATH, source='local')
        PLATE_MODEL = torch.hub.load(YOLOV5_PATH, 'custom', path=PLATE_MODEL_PATH, source='local')
        
        # 각 모델의 신뢰도 임계값(confidence threshold) 설정
        WINDOW_MODEL.conf = 0.25
        STICKER_MODEL.conf = 0.25
        PLATE_MODEL.conf = 0.4 # 번호판은 더 높은 신뢰도가 필요할 수 있음
        
        print("YOLOv5 모델 로드 완료.")
    except Exception as e:
        print(f"모델 로드 중 오류 발생: {e}")
        # 모델 로드 실패 시 종료
    #exit()

# --- 유틸리티 함수 ---
def allowed_file(filename):
    """파일 이름에 확장자가 포함되어 있고, 허용된 확장자인지 확인"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def find_largest_box(detections):
    """탐지된 객체들 중 가장 면적이 큰 객체를 찾음"""
    if not detections:
        return None
    
    for box in detections:
        x1, y1, x2, y2 = box['coords']
        box['area'] = (x2 - x1) * (y2 - y1)
        
    return max(detections, key=lambda x: x['area'])

# --- 🚀 최적화 1: subprocess 대신 PyTorch를 직접 사용하는 추론 함수 ---
def perform_detection(model, image_np):
    """
    미리 로드된 YOLOv5 모델과 Numpy 이미지 배열을 사용하여 추론을 수행합니다.
    """
    image_rgb = cv2.cvtColor(image_np, cv2.COLOR_BGR2RGB)#RGB변환하기
    
    # 모델에 RGB 이미지 전달하여 결과 받기
    results = model(image_rgb)
    
    # 결과를 pandas DataFrame 형태로 변환하여 처리
    detections_df = results.pandas().xyxy[0]
    
    detections = []
    # DataFrame의 각 행을 순회하며 결과 파싱
    for _, row in detections_df.iterrows():
        detections.append({
            'class': int(row['class']),
            'coords': [int(row['xmin']), int(row['ymin']), int(row['xmax']), int(row['ymax'])],
            'confidence': row['confidence']
        })
    return detections

# (OCR 관련 함수들은 기존 방식 유지)
def extract_json_result_from_output(output_text):
    if not output_text:
        return None
    lines = output_text.strip().split('\n')
    json_start = False
    for line in lines:
        if "=== RESULT_JSON ===" in line:
            json_start = True
            continue
        if json_start and line.strip():
            try:
                return json.loads(line.strip())
            except json.JSONDecodeError:
                continue
    return None

def run_ocr_with_conda_run(image_path, conda_env_name="ocr"):
    try:
        ocr_script_path = "/workspace/capston2/flask_app/paddletest.py"#어차피 안쓸거라 수정안
        result = subprocess.run([
            "conda", "run",
            "-n", conda_env_name,
            "python", ocr_script_path, image_path
        ], capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            print(f"OCR Error: {result.stderr}")
            return None

        return extract_json_result_from_output(result.stdout)
        
    except subprocess.TimeoutExpired:
        print("OCR 실행 시간 초과")
        return None
    except Exception as e:
        print(f"OCR 실행 중 오류 발생: {e}")
        return None
#입력 처리 라우트
@model_app.route('/upload', methods=['POST'])
def upload_image():
    # --- 🚀 수정: 폼 데이터와 파일 동시 처리 ---
    if 'file' not in request.files:
        return jsonify({'error': '업로드할 파일이 없습니다.'}), 400

    file = request.files['file']

    # 폼 데이터 받기
    machine_id = request.form.get('machine_id')
    zone = request.form.get('zone')  # .get()은 키가 없으면 None을 반환
    time = request.form.get('time')

    # 필수 폼 데이터 확인 (zone은 선택 사항)
    if not machine_id or not time:
        return jsonify({'error': 'machine_id와 time은 필수 항목입니다.'}), 400
    # --- 🚀 수정 끝 ---

    if file.filename == '':
        return jsonify({'error': '선택된 파일이 없습니다.'}), 400
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
    
        
        try:
            # --- 🚀 최적화 2: 이미지를 디스크에 저장하지 않고 메모리에서 바로 처리 ---
            # 파일 스트림을 읽어 OpenCV가 처리할 수 있는 Numpy 배열로 변환
            filestr = file.read()
            npimg = np.frombuffer(filestr, np.uint8)
            original_image_np = cv2.imdecode(npimg, cv2.IMREAD_COLOR)

            # 1. 창문 감지 (메모리에 로드된 모델 사용)
            print("1. 창문 감지 실행...")
            window_detections = perform_detection(WINDOW_MODEL, original_image_np)
            
            if not window_detections:
                return jsonify({
                    'success': True,
                    'detection': 'no_window',
                    'message': '창문이 감지되지 않았습니다.'
                }), 200

            # 2. 가장 큰 창문 영역 찾기
            largest_window = find_largest_box(window_detections)
            if not largest_window: # 혹시 모를 예외 처리
                 return jsonify({'detection': 'no_window', 'message': '창문 영역을 찾을 수 없습니다.'}), 200

            # --- 🚀 최적화 2: Numpy 배열을 직접 슬라이싱하여 이미지 자르기 ---
            wx1, wy1, wx2, wy2 = largest_window['coords']
            window_image_np = original_image_np[wy1:wy2, wx1:wx2]

            # 3. 잘라낸 창문 이미지에서 스티커 감지
            print("2. 스티커 감지 실행...")
            sticker_detections = perform_detection(STICKER_MODEL, window_image_np)
            
            # 최종 결과 JSON 준비
            result = {
                'success': True,
                'filename': filename,
                'window': {
                    'coords': largest_window['coords'],
                    'confidence': largest_window['confidence']
                }
            }

            # 4. 스티커 감지 결과에 따라 분기
            if sticker_detections:
                print("스티커 감지됨. 정상 차량으로 판단.")
                stickers = []
                for sticker in sticker_detections:
                    # 창문 내 상대 좌표를 원본 이미지의 절대 좌표로 변환
                    sx1_rel, sy1_rel, sx2_rel, sy2_rel = sticker['coords']
                    stickers.append({
                        'coords': [wx1 + sx1_rel, wy1 + sy1_rel, wx1 + sx2_rel, wy1 + sy2_rel],
                        'confidence': sticker['confidence'],
                        'class': sticker['class']
                    })
                
                result['stickers'] = stickers
                # send_mms_via_aligo(filepath, "위반하지 않았습니다", "") # MMS 전송 시 파일 경로 필요
            
            else:
                print("스티커 미감지. 위반 차량으로 판단. 번호판 인식 시작...")
                result['stickers'] = []
                result['message'] = '창문은 감지되었으나 스티커는 감지되지 않았습니다.'
                try:
                    # 1. 위반 이미지 저장
                    # app.config['VIOLATION_FOLDER']는 'violations'
                    os.makedirs(current_app.config['VIOLATION_FOLDER'], exist_ok=True)
                    
                    # 파일명에 포함될 수 없는 문자(콜론, 공백)를 안전하게 변경
                    safe_time = time.replace(':', '-').replace(' ', '_')
                    # 유니크한 파일명 생성 (시간_머신ID_원본파일명)
                    violation_filename = f"{safe_time}_{machine_id}_{filename}"
                    
                    # 저장할 상대 경로
                    relative_path = os.path.join(current_app.config['VIOLATION_FOLDER'], violation_filename)
                    
                    # 원본 이미지(Numpy 배열)를 파일로 저장
                    cv2.imwrite(relative_path, original_image_np)
                    print(f"위반 이미지 저장 완료: {relative_path}")

                    # 2. 데이터베이스에 정보 저장
                    conn = sqlite3.connect(DATABASE)
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO violations (machine_id, zone, time, image)
                        VALUES (?, ?, ?, ?)
                    """, (machine_id, zone, time, relative_path))
                    conn.commit()
                    conn.close()
                    print("데이터베이스에 위반 정보 저장 완료.")

                    # 3. 결과 JSON에 저장 정보 추가
                    result['violation_data'] = {
                        'saved_image': relative_path
                    }

                except Exception as e:
                    print(f"위반 정보(파일/DB) 저장 중 오류 발생: {e}")
                    # 에러가 발생해도 일단 처리는 계속하되, 결과에 에러 명시
                    result['violation_error'] = f"DB 또는 파일 저장 실패: {str(e)}"
                # --- 🚀 수정 끝 ---
                # 5. (스티커 없을 시) 원본 이미지에서 번호판 감지
                plate_detections = perform_detection(PLATE_MODEL, original_image_np)
                
                if plate_detections:
                    largest_plate = find_largest_box(plate_detections)
                    px1, py1, px2, py2 = largest_plate['coords']
                    plate_image_np = original_image_np[py1:py2, px1:px2]

                    # OCR은 파일 경로가 필요하므로, 잘라낸 번호판 이미지를 임시 저장
                    #os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
                    #plate_image_path = os.path.join(app.config['UPLOAD_FOLDER'], f"plate_{filename}")
                    #cv2.imwrite(plate_image_path, plate_image_np)
                    
                    #print("3. OCR 실행...")
                    #ocr_result = run_ocr_with_conda_run(plate_image_path)
                    #result['carnumber'] = ocr_result
                    
                    # 임시 파일 삭제
                    #os.remove(plate_image_path)
                else:
                    result['carnumber'] = None
                
                # send_mms_via_aligo(filepath, "위반감지", "") # MMS 전송 시 파일 경로 필요

            return jsonify(result), 200
            
        except Exception as e:
            import traceback
            traceback_str = traceback.format_exc()
            return jsonify({
                'error': f'처리 중 오류가 발생했습니다: {str(e)}', 
                'traceback': traceback_str
            }), 500
    
    return jsonify({'error': '허용되지 않은 파일 형식입니다.'}), 400

#if __name__ == '__main__':
    # Gunicorn 같은 WSGI 서버 사용을 권장합니다.
    # 개발용으로 실행 시:
#    app.run(host='0.0.0.0', port=5000, debug=False) # 운영 시 debug=False