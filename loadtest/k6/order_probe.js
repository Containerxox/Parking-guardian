// 대조 실험(로컬 모의 서버 전용): arrival-rate 실행기로 "장비별 순서"를 만들려는 순진한 방식이 왜 안 되는지 보여 준다.
// 장비 d, seq 는 전역 반복 번호로 정한다(iterationInTest). 반복은 아무 VU 에나 배정되고 응답을 기다리지 않고 겹치므로
// 같은 장비의 요청이 동시에 날아가고 서버에는 seq 가 뒤섞여 도착한다. scenarios_p2.js 는 이 방식을 쓰지 않는다.
import http from 'k6/http';
import exec from 'k6/execution';
import { env, num, required, PHOTOS } from './lib.js';
import { runTag, makeId } from './lib_p2.js';

const BASE = required('BASE_URL').replace(/\/$/, '');
const KEYS = required('DEVICE_KEYS').split(',');
export const options = {
  scenarios: { naive: { executor: 'constant-arrival-rate', rate: num('RATE', 8), timeUnit: '1s', duration: `${num('SECONDS', 15)}s`,
    preAllocatedVUs: 20, maxVUs: 60 } },
};
const tag = runTag(env('RUN_ID', 'probe'), '0badc0de');
export default function () {
  const n = exec.scenario.iterationInTest;
  const dev = n % KEYS.length;
  const seq = Math.floor(n / KEYS.length) + 1;
  http.post(`${BASE}/api/v1/device/detection-jobs`, {
    file: http.file(PHOTOS.sample_a, 'sample_a.jpg', 'image/jpeg'), event: seq === 1 ? 'arrived' : 'refresh',
    event_id: makeId(tag, 'e', dev, 0, seq), session_id: makeId(tag, 'a', dev, 0, 0), seq: String(seq),
  }, { headers: { 'X-Device-Key': KEYS[dev] } });
}
