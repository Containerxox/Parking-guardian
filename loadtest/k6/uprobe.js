// U-probe (P0-c): Wi-Fi 업로드 용량 프로브. 읽을 수 없는 "이미지"(무작위 바이트)를 POST /device/detections 로 보낸다.
// 서버는 inference 디코딩 실패 -> 400 을 반환한다(record_verdict, 사진 저장, 알림 이전). 상세: eks/uprobe-safety-review.md
// 환경변수: BASE_URL, DEVICE_KEY(값은 로그·명령줄에 남기지 않는다), PHASE(smoke1|smoke20|U1|U2), RATE_X100(초당 요청 수 x100), DUR(초), RUN_ID
import http from 'k6/http';
import { Counter } from 'k6/metrics';
import { check } from 'k6';
import { env, num, required } from './lib.js';

const PHASE = env('PHASE', 'smoke1');
const SIZE = num('SIZE', 369857); // sample_a 와 같은 크기
const RATE_X100 = num('RATE_X100', 100); // 초당 요청 수 x 100 (정수 요구)
const DUR = num('DUR', 20);
const RUN_ID = env('RUN_ID', 'uprobe');
const BASE_URL = required('BASE_URL');
const DEVICE_KEY = required('DEVICE_KEY');

const non400 = new Counter('probe_non_400');
const got400 = new Counter('probe_400');

const body = new Uint8Array(SIZE);
for (let i = 0; i < SIZE; i++) body[i] = Math.floor(Math.random() * 256);

export const options = {
  scenarios: PHASE === 'smoke1'
    ? { probe: { executor: 'shared-iterations', vus: 1, iterations: 1, maxDuration: '60s' } }
    : { probe: { executor: 'constant-arrival-rate', rate: RATE_X100, timeUnit: '100s', duration: `${DUR}s`,
                 preAllocatedVUs: 40, maxVUs: 120 } },
  discardResponseBodies: true,
  insecureSkipTLSVerify: false,
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
};

export default function () {
  const res = http.post(`${BASE_URL}/api/v1/device/detections`, { file: http.file(body.buffer, 'probe.jpg', 'image/jpeg') }, {
    headers: { 'X-Device-Key': DEVICE_KEY, 'User-Agent': `pg-uprobe-${RUN_ID}` },
    timeout: '30s',
    tags: { kind: 'probe', phase: PHASE },
  });
  if (res.status === 400) got400.add(1); else non400.add(1);
  check(res, { 'status 400': (r) => r.status === 400 });
}
