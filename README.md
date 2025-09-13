# 리액트 + flask 프로젝트 3조

## 간단 사용법

### 환경설정
우선 pc에 vscode와 python,nodejs가 설치되어있어야함(python버전은 3.3이상)
설치가 되어있다면 프로젝트를 다운받자

다운로드 받는 방법은
폴더를 하나 만들고
vscode를 켜고 방금 만들 폴더를 연 다음 terminal을 켜고 git clone -b 브랜치이름 https://github.com/minami-kotori-chan/capston.git
(**이때 브랜치 이름은 자기걸로 넣기 다른 사람꺼 넣으면 대형 사고다.**)
(참고 git clone https://github.com/minami-kotori-chan/capston.git 을 하게되면 모든 프로젝트가 다운로드 되므로 용량이 커지니까 추천하지 않음)

### `npm install`
이제 react 라이브러리에서 필요한걸 다운로드 받아야한다.
vscode에서 다운받은 폴더를 열고 terminal을 켠다.
터미널에 `cd frontend`를 입력한다.
`npm install`을 입력한다.
잠시 기다리면 완료

### `npm run`
잘 설치되었는지 확인하기 위해서 터미널에 `npm run`을 입력한다.
실행되면 브라우저에 자동으로 페이지가 열린다.
(참고로 실행하는데 오래걸린다)

### `python -m venv venv`
이제 다음으로 파이썬 환경을 설치해야한다.이를 위해 우선 파이썬 가상환경을 만드는게 추천된다.
가능하면 이과정도 해주도록하자.
터미널에서 경로를 backend로 이동해주자
(잘 모르겠으면 터미널 새로열고 cd backend 입력)
그다음 터미널에 `python -m venv venv`입력
이것도 좀 기다리면 완료되는데
그 이후 `.\venv\Scripts\Activate`를 입력하자
(윈도우 기준임 mac의 경우 `source venv/bin/activate`)

이러면 가상환경이 활성화 된거다.

### `pip insatall -r requirements.txt`
**가상환경이 활성화 된 상태에서 입력해야함**
`pip insatall -r requirements.txt`를 입력해주자 그러면 모든 필요한 라이브러리들이 설치된다.
만약 본인이 작업한 다음 새로운 라이브러리들을 pip를 통해서 설치한 경우

`pip freeze > requirements.txt`를 커밋전에 입력할것 **반드시 해야함**

### `python app.py`
backend폴더로 이동해서 python app.py를 입력한다.
이건 실행하고 자동으로 웹브라우저가 켜지지 않으니 웹브라우저를 켜고 주소창에 
localhost::5000을 입력해주자

## 커밋방법

### 변경사항이 생겼으면 vscode의 3번째 버튼 클릭
3번째 버튼에 보면 여러가지 버튼이 있는데
기본적으로 잘 모르겠으면 그냥 텍스트박스에 이번에 작업한 내용(간단하게)적고 커밋버튼 누르자
그리고 커밋을 하고 나면 변경 내용 동기화라고 있는데 이건 자주해도되고 맨마지막에 해도 상관없다 언젠가 하기만 하면 된다.


### 커밋 자주할것
프로젝트에서 기능하나 구현했다면 커밋하는걸 추천(100줄정도 추가했다면 하는게 좋음)
