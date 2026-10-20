import requests
import time 
import torch
import cv2
import numpy as np
import os
import io
import random
from picamera2 import Picamera2 
from datetime import datetime
import RPi.GPIO as GPIO

Picamera2.set_logging(Picamera2.ERROR) 
os.environ["LIBCAMERA_LOG_LEVELS"] = "3"

GPIO.setmode(GPIO.BCM)

LED = 4
GPIO.setup(LED, GPIO.OUT)

def turn_on_led(color):
    if color == 'ON':
        GPIO.output(LED, True)
    elif color == 'OFF':
        GPIO.output(LED, False)   

def get_pi_serial(): #라즈베리파이 시리얼번호 추출
    serial = "Unknown"
    try:
        with open('/proc/cpuinfo', 'r') as f:
            for line in f:
                if line.strip().startswith('Serial'):
                    serial = line.strip().split(':')[-1].strip()
                    break
    except Exception as e:
        serial = f"Error: {e}"
    return serial

#serial_number = get_pi_serial()
serial_number = ["100000005131f079", "100000009281fd90", "1000000019n31s26"] #테스트용 시리얼코드
user_id = "user1"

data = {
        "user_id": user_id,
        "serial": random.choice(serial_number),
        "zone": "A구역",
        "time": ""
    }

url = 'http://<서버 주소>/api/upload'  # 예전 서버 주소. 실제 주소를 넣어 쓴다

model = torch.hub.load('ultralytics/yolov5', 'yolov5m') # yolov5m 모델 사용

camera = Picamera2() # 카메라 설정
camera_config=camera.create_preview_configuration({"size": (3280,2464)})
camera.configure(camera_config)
camera.set_controls({
    "AeEnable": True,
    "AnalogueGain": 8.0,
    "ExposureTime": 10000,
    "AwbEnable": True,
    "Contrast": 1.5,
    "Brightness": 0.1
    })
time.sleep(0.5)
camera.start()

turn_on_led('OFF')

while True:
    start_time = time.time() # 시작시간 저장

    try:
        now = datetime.now() # 현재 시간을 파일명으로 사용
        filename = now.strftime("/home/pi/pictures/%Y%m%d_%H%M%S.jpg")
        camera.capture_file(filename)
        time.sleep(2)

        print(f"\n{now.replace(microsecond=0)} captured\n")

        image = cv2.imread(filename) # yolov5m모델에 사용할 사진 설정, 조정
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB) # 보정

        results = model(image) # 추론

        vehicle_classes = [2, 3, 5, 7]        
        car_detected = False

        for i, (*box, conf, cls) in enumerate(results.xyxy[0]):
            if int(cls) in vehicle_classes and conf > 0.2:
                x1, y1, x2, y2 = map(int, box)
                cropped = image[y1:y2, x1:x2]
                            
                print("차량이 감지되었습니다!")
                #files = {'file': open(filename, 'rb')} 

                timestr = datetime.now().strftime("%Y%m%d_%H%M%S")
                cropped_car_filename = f"{timestr}_{i}.jpg"

                img_encode = cv2.imencode('.jpg', cropped)[1]
                file_bytes = io.BytesIO(img_encode.tobytes())
                files = {'file': (cropped_car_filename, file_bytes, 'image/jpeg')}

                data["time"] = now

                response = requests.post(url, files=files, data=data) # url로 사진, 데이터를 전송

                response_json = response.json() # 받은 응답 출력
                stickers = response_json.get("stickers", [])

                if "stickers" in response_json and stickers == []:
                    turn_on_led('ON')
                else:
                    turn_on_led('OFF')
                car_detected = True

        if car_detected == False:
            print("차량이 없습니다.") 
            turn_on_led('OFF')
                
    except Exception as e:
        print("error:", e)
        camera.stop()
        GPIO.cleanup()
    
    elapsed = (time.time() - start_time) # 현재시간을 불러와 작업한 시간을 기록
    print(f"processing time: {elapsed:.2f}s\n") # 걸린 시간을 출력

    sleeptime = 60 - elapsed # 60초 - 작업시간
    if sleeptime > 0: #시간이 남았으면 그 시간만큼 idle
        time.sleep(sleeptime)
    else: # 시간이 오버되면 오버된 시간을 알리고 바로 시작
        print(f"{abs(sleeptime):.2f}s exceeded")   

camera.stop()
GPIO.cleanup()