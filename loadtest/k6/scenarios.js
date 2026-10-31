// 전체 경로 부하: 로컬 PC → ALB → api → inference. 사전 등록 5.2 의 S0 ~ S5, X1, T2, V2/V3/P1 의 부하 프로필.
// (주입 실험 S5 와 V2/V3 의 kubectl 조작은 README 의 절차로 한다. 이 스크립트는 부하만 낸다.)
//
//   SCENARIO=S0   일반 API 5 req/s, 5분, 사진 0                              (기준선)
//   SCENARIO=S1   일반 API 5 req/s + 사진 0.2C → 0.5C → 0.8C → 1.0C → 1.2C → 1.5C (각 3분)   (T2, X1, P1 도 이 프로필)
//   SCENARIO=S2   일반 API 5 req/s + 사진 20건 동시 1회 뒤 유휴
//   SCENARIO=S3   일반 API 5 req/s + 사진 1.2C 10분 + 회복 관찰 3분
//   SCENARIO=S5   일반 API 5 req/s + 사진 0.5C 8분 (2분 정상 / 3분 주입 / 3분 회복. 주입 시각은 사람이 기록)
//
// C 는 E3 에서 잰 서비스 전체 처리 용량(장/초)이다: -e C=1.8.
// 항상 같이 도는 것: readyz 1 req/s (별도 태그 kind:readyz).
//
// 필수: BASE_URL(https://parking.<도메인>), ADMIN_EMAIL, ADMIN_PASSWORD, DEVICE_KEYS(쉼표로 구분한 장비 키).
// 보존 규칙: 장비 키는 활성 위반이 없는 machine 2, 4, 5 것만 쓴다(machine 1, 3 금지).
// 로컬 검증: -e SCALE=0.05 로 단계 길이를 줄인다(최소 5초). 실제 측정은 SCALE 을 주지 않는다.
import http from 'k6/http';
import exec from 'k6/execution';
import { check } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { env, num, required, PHOTOS, stepAt, buildSteps, scaled } from './lib.js';

const SCENARIO = env('SCENARIO', 'S0');
const C = num('C', 0);
const API_RATE = num('API_RATE', 5);
// 사진 중 sample_b(위반) 비율. 기본 0 = 허가 판정 사진(sample_a) 100% (사용자 결정 2026-10-31).
// 위반 사진은 DB·S3 쓰기와 알림을 만들므로 별도 시나리오·테스트 전용 데이터에서만 쓴다.
// 0 보다 크게 주려면 ALLOW_VIOLATIONS=1 을 명시해야 한다.
const VIOLATION_RATIO = num('VIOLATION_RATIO', 0);
if (VIOLATION_RATIO > 0 && env('ALLOW_VIOLATIONS', '') !== '1') {
  throw new Error('VIOLATION_RATIO > 0 은 위반 기록(DB·S3·알림)을 만듭니다. 정말 필요하면 -e ALLOW_VIOLATIONS=1 을 같이 주세요.');
}
const BASE = required('BASE_URL').replace(/\/$/, '');
const API = `${BASE}/api/v1`;
const KEYS = required('DEVICE_KEYS').split(',').map((k) => k.trim()).filter(Boolean);
const ADMIN_EMAIL = required('ADMIN_EMAIL');
const ADMIN_PASSWORD = required('ADMIN_PASSWORD');

const photoOk = new Rate('photo_ok');              // 장비가 200/201 을 받은 비율 (통과 기준 "사진 성공률")
const photoMs = new Trend('photo_ms', true);
// 상태별 개수(태그 없는 별도 지표라 요약 JSON 에 그대로 나온다). 장비가 보는 503 은 하나의 코드(inference_unavailable)라
// k6 는 busy / 마감 초과 / 모델 없음을 구분할 수 없다. 그 구분은 서버의 pg_ai_client_requests_total{outcome} 로 한다.
const photo2xx = new Counter('photo_http_2xx');
const photo503 = new Counter('photo_http_503');
const photoOther = new Counter('photo_http_other'); // 401, 400, 500, 연결 실패(status 0) 등. 0 이어야 정상
const apiOk = new Rate('api_ok');
const apiMs = new Trend('api_ms', true);
const readyzMs = new Trend('readyz_ms', true);

// ---- 시나리오별 사진 부하 정의 -------------------------------------------------------------------
const MIN = 60;
function profile() {
  if (SCENARIO === 'S0') {
    const total = scaled(5 * MIN);
    return { total, photoStages: null, steps: buildSteps([{ name: 'baseline', seconds: total }]) };
  }


  if (SCENARIO !== 'S2' && C <= 0) {
    throw new Error('S1, S3, S5 는 -e C=<장/초> 가 필요합니다 (E3 에서 잰 서비스 전체 처리 용량).');
  }

  if (SCENARIO === 'S1') {
    const mult = [0.2, 0.5, 0.8, 1.0, 1.2, 1.5];
    const parts = mult.map((m) => ({ name: `${m.toFixed(1)}C`, seconds: scaled(3 * MIN), rate: C * m }));
    return { total: parts.length * scaled(3 * MIN), photoStages: parts, steps: buildSteps(parts) };
  }
  if (SCENARIO === 'S3') {
    const parts = [
      { name: '1.2C', seconds: scaled(10 * MIN), rate: C * 1.2 },
      { name: 'recovery', seconds: scaled(3 * MIN), rate: 0 },
    ];
    return { total: parts[0].seconds + parts[1].seconds, photoStages: parts, steps: buildSteps(parts) };
  }
  if (SCENARIO === 'S5') {
    const parts = [
      { name: 'normal', seconds: scaled(2 * MIN), rate: C * 0.5 },
      { name: 'inject', seconds: scaled(3 * MIN), rate: C * 0.5 },
      { name: 'recover', seconds: scaled(3 * MIN), rate: C * 0.5 },
    ];
    return { total: parts.reduce((a, p) => a + p.seconds, 0), photoStages: parts, steps: buildSteps(parts) };
  }
  if (SCENARIO === 'S2') {
    // 60초 정상 → 20건 동시 → 120초 유휴
    const before = scaled(60);
    const after = scaled(120);
    return { total: before + after, photoStages: null, burst: { at: before, count: num('BURST', 20) },
      steps: buildSteps([{ name: 'before', seconds: before }, { name: 'burst+idle', seconds: after }]) };
  }
  throw new Error(`알 수 없는 SCENARIO=${SCENARIO} (S0, S1, S2, S3, S5)`);
}

const plan = profile();

// ramping-arrival-rate 는 단계 목표가 정수(단위시간당)라 10초 단위로 둔다. 단계 사이 기울기를 피하려고 1초 점프 뒤 유지.
function photoExecutor(parts) {
  const stages = [];
  for (const p of parts) {
    const target = Math.round(p.rate * 10);
    stages.push({ target, duration: '1s' }, { target, duration: `${p.seconds - 1}s` });
  }
  return {
    executor: 'ramping-arrival-rate', startRate: Math.round(parts[0].rate * 10), timeUnit: '10s', stages,
    preAllocatedVUs: num('PHOTO_VUS', 30), maxVUs: num('PHOTO_MAX_VUS', 80), exec: 'photo', tags: { kind: 'photo' },
  };
}

const scenarios = {
  readyz: {
    executor: 'constant-arrival-rate', rate: 1, timeUnit: '1s', duration: `${plan.total}s`,
    preAllocatedVUs: 2, maxVUs: 5, exec: 'readyz', tags: { kind: 'readyz' },
  },
  api: {
    executor: 'constant-arrival-rate', rate: API_RATE, timeUnit: '1s', duration: `${plan.total}s`,
    preAllocatedVUs: num('API_VUS', 20), maxVUs: num('API_MAX_VUS', 60), exec: 'api', tags: { kind: 'api' },
  },
};
if (plan.photoStages) scenarios.photo = photoExecutor(plan.photoStages);
if (plan.burst) {
  scenarios.photo = {
    executor: 'per-vu-iterations', vus: plan.burst.count, iterations: 1, maxDuration: '3m',
    startTime: `${plan.burst.at}s`, exec: 'photo', tags: { kind: 'photo' },
  };
}

// 단계별 서브메트릭을 요약에 남기려고 단계마다 자리표시 임계값을 둔다 (판정 아님).
const stepThresholds = {};
for (const step of plan.steps) {
  stepThresholds[`photo_ok{step:${step.name}}`] = ['rate>=0'];
  stepThresholds[`api_ms{step:${step.name}}`] = ['max>=0'];
  stepThresholds[`photo_http_503{step:${step.name}}`] = ['count>=0'];
}

export const options = {
  scenarios,
  setupTimeout: '10m',
  thresholds: {
    // 통과 기준 (사전 등록 3절). abortOnFail 은 쓰지 않는다: 기준을 못 맞춘 회차도 끝까지 기록한다.
    // 사진 성공률 기준(0.8C 이하 ≥ 99%)은 단계별 값으로 사후 판정한다 (전체 값은 1.0C 이상이 섞인다).
    api_ms: ['p(95)<500'],
    api_ok: ['rate>0.99'],
    // 무효 판정: 부하 발생기가 목표 도착률을 못 냈다
    dropped_iterations: ['count==0'],
    // 사진 요청이 401/400/500/연결 실패면 부하 문제가 아니라 스크립트·설정 문제다. 있으면 그 회차는 무효로 본다.
    photo_http_other: ['count==0'],
    ...stepThresholds,
  },
};

// ---- 일반 API --------------------------------------------------------------------------------
// 로그인은 VU 마다 한 번(토큰 만료 15분 전에 401 이 오면 다시). 로그인 요청은 통계에서 kind:login 으로 분리한다.
let token = null;
function login() {
  const res = http.post(`${API}/auth/login`, JSON.stringify({ email: ADMIN_EMAIL, password: ADMIN_PASSWORD }),
    { headers: { 'Content-Type': 'application/json' }, tags: { kind: 'login' } });
  token = res.status === 200 ? res.json('access_token') : null;
  return token;
}

const API_PATHS = ['/violations?size=20', '/dashboard/summary', '/notifications'];

// 단계 판정은 setup() 이 끝난 시각(data.t0)부터 잰다. 워밍업이 오래 걸려도 단계가 밀리지 않는다.
export function api(data) {
  const step = stepAt(plan.steps, Date.now() - data.t0);
  if (!token && !login()) {
    apiOk.add(false, { step });
    return;
  }
  const path = API_PATHS[exec.scenario.iterationInTest % API_PATHS.length];
  const res = http.get(`${API}${path}`, {
    headers: { Authorization: `Bearer ${token}` }, tags: { kind: 'api', route: path.split('?')[0], step },
  });
  if (res.status === 401) token = null; // 만료. 다음 반복에서 다시 로그인
  apiOk.add(res.status === 200, { step });
  apiMs.add(res.timings.duration, { step });
}

export function readyz() {
  const res = http.get(`${API}/readyz`, { tags: { kind: 'readyz' } });
  readyzMs.add(res.timings.duration);
  check(res, { 'readyz 200': (r) => r.status === 200 });
}

// ---- 사진 (장비 흉내) ---------------------------------------------------------------------------
// 기본은 허가 판정 사진(sample_a)만 보낸다. 허가 판정은 위반·알림·S3 쓰기를 만들지 않고 machine 행(마지막 신호,
// 카운터)만 갱신한다. 같은 장비의 사진은 DB 행 잠금으로 한 줄로 처리된다 (장비 3개 = 같은 행을 3곳이 다툰다).
export function photo(data) {
  const step = stepAt(plan.steps, Date.now() - data.t0);
  const n = exec.scenario.iterationInTest;
  const key = KEYS[n % KEYS.length];
  const isViolation = ((n * 7919) % 100) / 100 < VIOLATION_RATIO; // 결정적 분배 (재현 가능)
  const body = isViolation ? PHOTOS.sample_b : PHOTOS.sample_a;
  const res = http.post(`${API}/device/detections`, {
    file: http.file(body, isViolation ? 'sample_b.jpg' : 'sample_a.jpg', 'image/jpeg'),
    detected_at: new Date().toISOString(),
  }, { headers: { 'X-Device-Key': key }, timeout: '30s', tags: { kind: 'photo', step } });
  const ok = res.status === 200 || res.status === 201;
  photoOk.add(ok, { step });
  photoMs.add(res.timings.duration, { step });
  if (ok) photo2xx.add(1, { step });
  else if (res.status === 503) photo503.add(1, { step });
  else photoOther.add(1, { step });
}

// ---- 시작 전 준비 ------------------------------------------------------------------------------
// 부하 시작 전에 한 번: (1) 서버가 준비됐는지 (2) 관리자 로그인 (3) 장비 키가 유효한지 (4) 사진 3장으로 모델을 데운다.
// 이 요청들은 위의 사용자 정의 지표(api_ms, photo_ok ...)에 들어가지 않고 phase:warmup 태그가 붙는다.
export function setup() {
  const ready = http.get(`${API}/readyz`, { tags: { phase: 'warmup' } });
  if (ready.status !== 200) throw new Error(`readyz 가 ${ready.status} 입니다. 서버가 준비되지 않았습니다.`);
  if (!login()) throw new Error('관리자 로그인에 실패했습니다 (ADMIN_EMAIL / ADMIN_PASSWORD 확인).');
  for (const key of KEYS) {
    const hb = http.post(`${API}/device/heartbeat`, null, { headers: { 'X-Device-Key': key }, tags: { phase: 'warmup' } });
    if (hb.status !== 200) throw new Error(`장비 키 하나가 거절되었습니다 (heartbeat ${hb.status}). DEVICE_KEYS 확인.`);
  }
  if (plan.photoStages || plan.burst) {
    for (let i = 0; i < num('WARMUP_PHOTOS', 3); i += 1) {
      http.post(`${API}/device/detections`, { file: http.file(PHOTOS.sample_a, 'sample_a.jpg', 'image/jpeg') },
        { headers: { 'X-Device-Key': KEYS[0] }, timeout: '120s', tags: { phase: 'warmup' } });
    }
  }
  return { t0: Date.now() };
}
