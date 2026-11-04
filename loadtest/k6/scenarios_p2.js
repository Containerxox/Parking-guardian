// Phase 2(비동기 접수) 전체 경로 부하: 로컬 PC → ALB → backend(202) → S3/DB/SQS → Worker. 정의와 사용법: loadtest/P2_TOOLS.md.
// Phase 1 의 scenarios.js 와 같은 일반 API 부하·도착률 프로필을 쓰되, 사진은 장비마다 "한 줄로" 보낸다.
//
//   P2_SCENARIO=S0     일반 API 만(사진 0)                     S1  0.2C…1.5C 각 3분 (Phase 1 S1 과 같은 프로필)
//   P2_SCENARIO=W      고정 도착률 W_RATE(C 의 배수) 를 W_SECONDS 동안    D  1.5C 60초 뒤 중단 + 소화 관찰
//   P2_SCENARIO=K      KEDA 반응(0.2C → 1.0C → 0.2C → 유휴)       C  유휴 뒤 한꺼번에(장비마다 BURST 장)
//   P2_SCENARIO=SMOKE  짧은 확인(STEPS 나 W_RATE/W_SECONDS 로 조절)    STEPS="이름:장/초:초,…" 로 직접 지정도 가능
//
// 장비 직렬화(필수 설계): 장비 하나 = scenario 하나 = VU 하나 = 반복 하나. 그 반복 안의 while 루프가 계획된 시각까지 sleep 한 뒤
//   http.post(동기 호출)를 보낸다. 한 VU 는 응답이 올 때까지 다음 요청을 못 보내므로 "같은 장비의 요청이 겹치는 일"이 구조적으로 없다.
//   arrival-rate 실행기는 반복을 아무 VU 에나 배정하므로 장비 순서를 보장하지 못해서 쓰지 않는다(order_probe.js 가 그것을 보여 준다).
//   seq, event_id 카운터는 그 VU 의 지역 변수라서 다른 VU 와 섞이지 않는다.
//
// 목표 도착률을 못 지키면(응답이 간격보다 오래 걸림) 그 슬롯은 건너뛰고 p2_dropped_slots 로 센다 → 그 회차는 무효(임계값).
// 모든 전송은 "P2SEND {json}" 한 줄로 로그에 남는다(--log-output=file=… --log-format=raw). p2_collect.py 가 DB 와 event_id 로 맞춘다.
//
// 필수: BASE_URL, DEVICE_KEYS(쉼표). 일반 API 부하(API_RATE>0)에는 ADMIN_EMAIL, ADMIN_PASSWORD. 비밀은 -e 로만 준다(출력하지 않는다).
import http from 'k6/http';
import exec from 'k6/execution';
import { sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { env, num, required, PHOTOS, scaled } from './lib.js';
import { runTag, makeId, buildProfile, parseSteps, withBounds, totalSeconds, deviceSlots, classify } from './lib_p2.js';

const SCENARIO = env('P2_SCENARIO', 'S0');
const C = num('C', 0);
const SCALE = num('SCALE', 1);
const API_RATE = num('API_RATE', SCENARIO === 'S0' ? 5 : 5);
const RUN_ID = env('RUN_ID', 'p2');
const OUT_DIR = env('OUT_DIR', `./p2-out/${RUN_ID}`);
const EVENT_MODE = env('EVENT_MODE', 'refresh'); // 첫 장은 arrived, 이후 refresh (실제 장비 흐름). 'arrived' 면 모두 arrived
const LATE_TOL_MS = num('LATE_TOL_MS', 1000);    // 계획 시각보다 이만큼 이상 늦으면 그 슬롯을 건너뛴다(도착률을 못 지킨 것)
const RETRY = num('RETRY', 0);                   // 실패 시 같은 event_id·seq 로 재전송하는 최대 횟수(기본 0: 재시도 없음 = 부하를 바꾸지 않음)
const TIMEOUT = env('REQ_TIMEOUT', '30s');
const WARMUP_PHOTOS = num('WARMUP_PHOTOS', 0);   // 장비마다 워밍업 사진 수(별도 세션·id 종류 b). DB 에 작업이 생긴다
const WARMUP_WAIT_S = num('WARMUP_WAIT_S', 0);   // 워밍업 뒤 판정이 끝나길 기다리는 시간(측정 t0 는 그 뒤)
const VIOLATION_RATIO = num('VIOLATION_RATIO', 0);
if (VIOLATION_RATIO > 0 && env('ALLOW_VIOLATIONS', '') !== '1') {
  throw new Error('VIOLATION_RATIO > 0 은 위반 기록(DB·S3·알림)을 만듭니다. 정말 필요하면 -e ALLOW_VIOLATIONS=1 을 같이 주세요.');
}
const BASE = required('BASE_URL').replace(/\/$/, '');
const API = `${BASE}/api/v1`;
const KEYS = SCENARIO === 'S0' ? [] : required('DEVICE_KEYS').split(',').map((k) => k.trim()).filter(Boolean);
const NDEV = KEYS.length;
const ADMIN_EMAIL = API_RATE > 0 ? required('ADMIN_EMAIL') : '';
const ADMIN_PASSWORD = API_RATE > 0 ? required('ADMIN_PASSWORD') : '';

// ---- 프로필 -------------------------------------------------------------------------------------
const steps = withBounds(env('STEPS', '')
  ? parseSteps(env('STEPS', ''), SCALE)
  : buildProfile(SCENARIO, { C, scale: SCALE, wRate: num('W_RATE', 0) || undefined, wSeconds: num('W_SECONDS', 0) || undefined, burst: num('BURST', 0) || undefined }));
const total = SCENARIO === 'S0' && steps.length === 0 ? scaled(300) : totalSeconds(steps);
if (SCENARIO !== 'S0' && NDEV === 0) throw new Error('DEVICE_KEYS 가 필요합니다');

// ---- 지표 ---------------------------------------------------------------------------------------
const acceptMs = new Trend('p2_accept_ms', true);
const acceptOk = new Rate('p2_accept_ok');            // 202 또는 200(duplicate) 비율
const sent = new Counter('p2_sent');                  // 단계별 전송 시도(재시도 제외)
const accepted = new Counter('p2_accepted');          // 202
const duplicate = new Counter('p2_duplicate');        // 200 + duplicate=true (재시도가 이미 접수된 건)
const http4xx = new Counter('p2_http4xx');
const http5xx = new Counter('p2_http5xx');
const other = new Counter('p2_other');                // 연결 실패·시간 초과(status 0) 등
const retries = new Counter('p2_retries');
const droppedSlots = new Counter('p2_dropped_slots'); // 목표 시각을 못 지켜 건너뛴 슬롯
const lateMs = new Trend('p2_late_ms', true);         // 전송 시각이 계획보다 늦은 정도
const apiOk = new Rate('api_ok');
const apiMs = new Trend('api_ms', true);

// ---- 시나리오 구성 ---------------------------------------------------------------------------------
const GRACE = num('GRACE_S', 120);
const scenarios = {
  readyz: { executor: 'constant-arrival-rate', rate: 1, timeUnit: '1s', duration: `${total}s`, preAllocatedVUs: 2, maxVUs: 5, exec: 'readyz', tags: { kind: 'readyz' } },
};
if (API_RATE > 0) {
  scenarios.api = { executor: 'constant-arrival-rate', rate: API_RATE, timeUnit: '1s', duration: `${total}s`,
    preAllocatedVUs: num('API_VUS', 20), maxVUs: num('API_MAX_VUS', 60), exec: 'api', tags: { kind: 'api' } };
}
for (let d = 0; d < NDEV && steps.length > 0; d += 1) {
  // 장비 하나당 VU 하나, 반복 하나. 루프는 device() 안에 있다.
  scenarios[`dev${d}`] = { executor: 'per-vu-iterations', vus: 1, iterations: 1, maxDuration: `${total + GRACE + WARMUP_WAIT_S + 60}s`,
    exec: 'device', tags: { kind: 'photo' } };
}

const stepThresholds = {};
for (const s of steps) {
  stepThresholds[`p2_sent{step:${s.name}}`] = ['count>=0'];
  stepThresholds[`p2_accepted{step:${s.name}}`] = ['count>=0'];
  stepThresholds[`p2_accept_ms{step:${s.name}}`] = ['max>=0'];
  stepThresholds[`p2_http5xx{step:${s.name}}`] = ['count>=0'];
  stepThresholds[`p2_other{step:${s.name}}`] = ['count>=0'];
}
const thresholds = {
  p2_dropped_slots: ['count==0'],   // 무효 판정: 목표 도착률을 못 냈다
  p2_other: ['count==0'],           // 연결 실패·시간 초과: 장비 직렬화 가정(응답이 온 뒤 다음 요청)이 깨질 수 있다
  p2_http4xx: ['count==0'],         // 400/401/404/409: 스크립트·설정 문제
  dropped_iterations: ['count==0'],
  ...stepThresholds,
};
if (API_RATE > 0) { thresholds.api_ms = ['p(95)<500']; thresholds.api_ok = ['rate>0.99']; }
if (env('ABORT_ON_ERRORS', '') === '1') thresholds.p2_http5xx = [...(thresholds.p2_http5xx || []), { threshold: 'count<50', abortOnFail: true, delayAbortEval: '10s' }];
export const options = { scenarios, setupTimeout: '10m', thresholds };

// ---- 일반 API (Phase 1 과 같은 요청) ----------------------------------------------------------------
let token = null;
function login() {
  const res = http.post(`${API}/auth/login`, JSON.stringify({ email: ADMIN_EMAIL, password: ADMIN_PASSWORD }),
    { headers: { 'Content-Type': 'application/json' }, tags: { kind: 'login' } });
  token = res.status === 200 ? res.json('access_token') : null;
  return token;
}
const API_PATHS = ['/violations?size=20', '/dashboard/summary', '/notifications'];
export function api(data) {
  const sec = (Date.now() - exec.scenario.startTime) / 1000;
  const step = (steps.find((s) => sec >= s.start && sec < s.end) || { name: 'after' }).name;
  if (!token && !login()) { apiOk.add(false, { step }); return; }
  const path = API_PATHS[exec.scenario.iterationInTest % API_PATHS.length];
  const res = http.get(`${API}${path}`, { headers: { Authorization: `Bearer ${token}` }, tags: { kind: 'api', route: path.split('?')[0], step } });
  if (res.status === 401) token = null;
  apiOk.add(res.status === 200, { step });
  apiMs.add(res.timings.duration, { step });
}
export function readyz() { http.get(`${API}/readyz`, { tags: { kind: 'readyz' } }); }

// ---- 장비 (한 VU = 한 장비) -------------------------------------------------------------------------
function post(key, fields, image, name) {
  return http.post(`${API}/device/detection-jobs`, { file: http.file(image, name, 'image/jpeg'), ...fields },
    { headers: { 'X-Device-Key': key }, timeout: TIMEOUT, tags: { kind: 'photo' } });
}

function pick(counter) { // 결정적 분배(재현 가능): 위반 사진 비율
  return ((counter * 7919) % 100) / 100 < VIOLATION_RATIO;
}

export function device(data) {
  const dev = Number(exec.scenario.name.replace('dev', ''));
  const key = KEYS[dev];
  const session = makeId(data.tag, 'a', dev, 0, 0);
  const slots = deviceSlots(data.steps, NDEV, dev);
  // 기준 시각은 setup() 이 끝난 시각(data.t0)이 아니라 "이 시나리오가 실제로 시작한 시각"이다. k6 가 시나리오를 늦게 시작해도(느린 PC, 초기화 지연)
  // 앞쪽 슬롯이 전부 "너무 늦은 것"으로 버려지지 않는다. 모든 시나리오는 같은 시작 시각을 공유하므로 단계가 장비 사이에 어긋나지 않는다.
  const t0 = exec.scenario.startTime;
  console.log(`P2START ${JSON.stringify({ dev, scenario_start: t0, now: Date.now(), start_delay_ms: Date.now() - t0, setup_t0: data.t0 })}`);
  let seq = 0;
  let counter = 0;
  for (const slot of slots) {
    const target = t0 + slot.t * 1000;
    const step = data.steps[slot.step];
    if (!slot.burst) {
      const now = Date.now();
      if (now > target + LATE_TOL_MS) { // 목표 시각을 못 지켰다: 건너뛰고 센다(seq·event_id 는 쓰지 않는다)
        droppedSlots.add(1, { step: step.name });
        console.log(`P2DROP ${JSON.stringify({ dev, step: step.name, late_ms: now - target })}`);
        continue;
      }
      if (now < target) sleep((target - now) / 1000);
    } else if (Date.now() < target) {
      sleep((target - Date.now()) / 1000); // burst 는 단계 시작 시각을 기다린 뒤, 응답이 오는 대로 연속 전송
    }
    seq += 1;
    const eid = makeId(data.tag, 'e', dev, slot.step, counter);
    counter += 1;
    const event = seq === 1 || EVENT_MODE === 'arrived' ? 'arrived' : 'refresh';
    const violation = pick(counter);
    const image = violation ? PHOTOS.sample_b : PHOTOS.sample_a;
    const fields = { event, event_id: eid, session_id: session, seq: String(seq), detected_at: new Date().toISOString() };
    const startedAt = Date.now();
    lateMs.add(Math.max(0, startedAt - target), { step: step.name });
    sent.add(1, { step: step.name });
    let attempt = 0;
    for (;;) { // 재시도는 같은 event_id·seq 로만 한다(서버는 중복을 200 duplicate 로 답한다). 기본 RETRY=0.
      const t1 = Date.now();
      const res = post(key, fields, image, violation ? 'sample_b.jpg' : 'sample_a.jpg');
      let body = null;
      try { body = res.json(); } catch (e) { body = null; }
      const kind = classify(res.status, body);
      acceptMs.add(res.timings.duration, { step: step.name });
      acceptOk.add(kind === 'accepted' || kind === 'duplicate', { step: step.name });
      if (kind === 'accepted') accepted.add(1, { step: step.name });
      else if (kind === 'duplicate') duplicate.add(1, { step: step.name });
      else if (kind === 'http4xx') http4xx.add(1, { step: step.name });
      else if (kind === 'http5xx') http5xx.add(1, { step: step.name });
      else if (kind !== 'ok200') other.add(1, { step: step.name });
      console.log(`P2SEND ${JSON.stringify({ dev, step: step.name, step_i: slot.step, eid, seq, event, attempt, ts: t1, status: res.status, kind, ms: Math.round(res.timings.duration), planned_ms: Math.round(target) })}`);
      const retryable = kind === 'http5xx' || kind === 'other';
      if (!retryable || attempt >= RETRY) break;
      attempt += 1;
      retries.add(1, { step: step.name });
      sleep(Math.min(8, 2 ** attempt * 0.5));
    }
  }
}

// ---- 시작 전 준비 ------------------------------------------------------------------------------
export function setup() {
  const ready = http.get(`${API}/readyz`, { tags: { phase: 'warmup' } });
  if (ready.status !== 200) throw new Error(`readyz 가 ${ready.status} 입니다. 서버가 준비되지 않았습니다.`);
  if (API_RATE > 0 && !login()) throw new Error('관리자 로그인에 실패했습니다 (ADMIN_EMAIL / ADMIN_PASSWORD 확인).');
  const tag = runTag(`${RUN_ID}|${Date.now()}`, env('RUN_TAG', ''));
  KEYS.forEach((key, d) => {
    const hb = http.post(`${API}/device/heartbeat`, null, { headers: { 'X-Device-Key': key }, tags: { phase: 'warmup' } });
    if (hb.status !== 200) throw new Error(`장비 키 하나가 거절되었습니다 (heartbeat ${hb.status}). DEVICE_KEYS 확인. (번호 ${d})`);
  });
  // 워밍업 사진: 측정과 다른 세션·id 종류(b). 서버의 첫 요청 비용(연결, 코드 경로)을 덜어 낸다. 측정 지표에는 넣지 않는다.
  for (let n = 0; n < WARMUP_PHOTOS; n += 1) {
    KEYS.forEach((key, d) => {
      const fields = { event: n === 0 ? 'arrived' : 'refresh', event_id: makeId(tag, 'b', d, 0, n), session_id: makeId(tag, 'a', d, 255, 0), seq: String(n + 1) };
      const res = http.post(`${API}/device/detection-jobs`, { file: http.file(PHOTOS.sample_a, 'sample_a.jpg', 'image/jpeg'), ...fields },
        { headers: { 'X-Device-Key': key }, timeout: '120s', tags: { phase: 'warmup' } });
      if (res.status !== 202 && res.status !== 200) throw new Error(`워밍업 사진이 거절되었습니다 (${res.status})`);
    });
  }
  if (WARMUP_WAIT_S > 0) sleep(WARMUP_WAIT_S);
  const t0 = Date.now();
  console.log(`P2META ${JSON.stringify({ run_id: RUN_ID, tag, t0, scenario: SCENARIO, c: C, scale: SCALE, ndev: NDEV, event_mode: EVENT_MODE, retry: RETRY, steps, total })}`);
  return { t0, tag, steps };
}

export function handleSummary(data) {
  const m = (name) => (data.metrics[name] && data.metrics[name].values) || {};
  const line = `p2: sent=${m('p2_sent').count || 0} accepted=${m('p2_accepted').count || 0} dup=${m('p2_duplicate').count || 0} `
    + `4xx=${m('p2_http4xx').count || 0} 5xx=${m('p2_http5xx').count || 0} other=${m('p2_other').count || 0} dropped_slots=${m('p2_dropped_slots').count || 0}\n`;
  return { stdout: line, [`${OUT_DIR}/summary.json`]: JSON.stringify(data, null, 1) };
}
