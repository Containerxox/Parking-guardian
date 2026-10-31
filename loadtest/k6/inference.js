// inference 단독 실험: E0 ~ E3, T1, (X1 의 AI 쪽). 클러스터 안 작은 k6 Job 으로 inference Service 를 직접 때린다.
//
//   MODE=closed  (기본) 동시 VUS 명이 응답을 받으면 바로 다음 요청. E0 ~ E3: 처리량과 장당 서비스 시간.
//   MODE=rate    도착률 RATE (장/초) 고정. T1: 1.2 × C₁ 로 INFERENCE_MAX_WAITING 후보를 비교.
//
// 예)
//   E0: k6 run -e INFERENCE_URL=http://inference:8000 -e RUN_ID=e0-1 inference.js
//   E3: k6 run -e VUS=6 -e DURATION=3m ... inference.js          (Pod 수 × 2 정도의 동시. README 참고)
//   T1: k6 run -e MODE=rate -e RATE=1.8 -e DURATION=3m ... inference.js
//
// 서버가 재는 값(pg_inference_*)은 Prometheus 에서 따로 가져온다. 여기 값은 클라이언트가 본 값이다.
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { env, num, PHOTOS } from './lib.js';

const MODE = env('MODE', 'closed');
const DURATION = env('DURATION', '3m');
const WARMUP = num('WARMUP_REQUESTS', 5); // 첫 추론은 느리다(콜드 스타트). setup() 에서 보내고 측정에서 뺀다.
const BASE = env('INFERENCE_URL', 'http://inference:8000');
const URL = `${BASE}/v1/analyze`;

const serviceMs = new Trend('inference_elapsed_ms', true); // 응답 본문의 elapsed_ms (서버가 잰 추론 시간)
const done = new Counter('inference_done');                // 200
const busy = new Counter('inference_busy');                // 503 {"error":"busy"}: 실행 1 + 대기 줄이 가득 차 즉시 거절 (정상 동작)
const unavailable = new Counter('inference_unavailable');  // 503 {"error":"model_unavailable"}: 모델 로딩 중/실패 (이상 상황)
const failed = new Counter('inference_failed');            // 그 밖의 실패 (0 이어야 정상)

const scenarios = {};
if (env('ITERATIONS', '')) {
  // 스모크 테스트용: 정확히 N 건만 보낸다 (처음 확인할 때 ITERATIONS=1).
  scenarios.measure = {
    executor: 'shared-iterations', vus: 1, iterations: num('ITERATIONS', 1), maxDuration: '5m',
    exec: 'measure', tags: { phase: 'measure' },
  };
} else if (MODE === 'rate') {
  scenarios.measure = {
    executor: 'constant-arrival-rate',
    rate: Math.round(num('RATE', 1) * 10), timeUnit: '10s', // 소수 도착률을 위해 10초 단위
    duration: DURATION,
    preAllocatedVUs: num('VUS', 20), maxVUs: num('MAX_VUS', 40),
    exec: 'measure', tags: { phase: 'measure' },
  };
} else {
  scenarios.measure = {
    executor: 'constant-vus', vus: num('VUS', 1), duration: DURATION,
    exec: 'measure', tags: { phase: 'measure' },
  };
}

export const options = {
  scenarios,
  setupTimeout: '10m',
  thresholds: {
    // 무효 판정(사전 등록 3절): 목표 도착률을 못 내면 그 회차는 무효. closed 모드엔 해당 없다.
    ...(MODE === 'rate' ? { dropped_iterations: ['count==0'] } : {}),
    // 요약에 항상 나오게 하는 자리표시(판정 아님). 워밍업은 setup() 에서 하고 이 지표들에는 들어가지 않는다.
    'http_req_duration{phase:measure}': ['max>=0'],
    inference_done: ['count>=0'],
    inference_busy: ['count>=0'],
    inference_unavailable: ['count>=0'],
    // 알 수 없는 응답이면 부하 문제가 아니라 설정 문제다. 그 회차는 무효.
    inference_failed: ['count==0'],
  },
};

// 장당 서비스 시간 분포를 보려고 a/b 를 번갈아 보낸다.
function pick(i) {
  return i % 2 === 0 ? ['sample_a', PHOTOS.sample_a] : ['sample_b', PHOTOS.sample_b];
}

function post(name, body, tags, timeout) {
  return http.post(URL, body, {
    headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': `${name}.jpg` },
    tags,
    timeout,
  });
}

// 워밍업: 부하 시작 전에 한 번만 실행된다. 모델이 준비될 때까지(/readyz 200) 기다린 뒤 WARMUP 장을 보낸다.
// 이 요청은 phase:warmup 태그이고 inference_* 사용자 지표에는 기록하지 않는다.
export function setup() {
  for (let i = 0; i < 120; i += 1) {
    if (http.get(`${BASE}/readyz`, { tags: { phase: 'warmup' } }).status === 200) break;
    sleep(2);
  }
  for (let i = 0; i < WARMUP; i += 1) {
    const [name, body] = pick(i);
    post(name, body, { phase: 'warmup' }, '120s');
  }
}

export function measure() {
  const [name, body] = pick(__ITER);
  const res = post(name, body, { photo: name, phase: 'measure' }, '60s');
  if (res.status === 200) {
    done.add(1);
    try {
      serviceMs.add(res.json('elapsed_ms'));
    } catch (e) {
      // 본문 형식이 달라도 처리량은 센다
    }
  } else if (res.status === 503 && String(res.body).includes('"busy"')) {
    busy.add(1);
  } else if (res.status === 503 && String(res.body).includes('model_unavailable')) {
    unavailable.add(1);
  } else {
    failed.add(1);
  }
  check(res, { '200 또는 503 busy': (r) => r.status === 200 || (r.status === 503 && String(r.body).includes('"busy"')) });
}
