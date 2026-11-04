// Phase 2 부하 테스트의 순수 함수 모음. k6 모듈을 가져오지 않아서 k6 안에서(test_lib_p2.js)도, 스크립트에서도 같은 코드로 쓴다.
// 설계 이유와 정의: loadtest/P2_TOOLS.md. 이 파일은 로컬 전용(Git 에 올리지 않는다).

// ---- 식별자 --------------------------------------------------------------------------------------
// event_id / session_id 는 UUID 여야 하고(서버가 UUID 로 파싱), 같은 (장비, event_id) 는 같은 작업이다(중복 접수 = 200 duplicate).
// 그래서 난수가 아니라 "구조가 있는 값"으로 만든다: 같은 실행 안에서 절대 겹치지 않고, 다른 실행(run tag)과도 겹치지 않는다.
//
//   RRRRRRRR-DDDD-4KSS-8000-CCCCCCCCCCCC
//   R: run tag 8 hex (실행마다 다름 — DB 에서 이번 실행의 행만 골라낸다)   D: 장비 번호 4 hex
//   K: 종류 1 hex  e = 측정 사진 이벤트, b = 워밍업 사진 이벤트, a = 세션
//   SS: 단계 번호 2 hex (측정 사진만. 그 사진이 어느 단계에서 보내졌는지가 id 에 들어간다)
//   C: 카운터 12 hex (장비·종류별 0 부터 1 씩)
const hex = (n, width) => {
  const s = Math.floor(n).toString(16);
  if (s.length > width) throw new Error(`값 ${n} 이 ${width} 자리 16진수를 넘습니다`);
  return s.padStart(width, '0');
};

export function fnv1a32(text) {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

// seed 는 RUN_ID 와 시작 시각 같은 것. RUN_TAG 를 직접 주면(8 hex) 그 값을 쓴다.
export function runTag(seed, override) {
  if (override) {
    if (!/^[0-9a-f]{8}$/.test(override)) throw new Error('RUN_TAG 는 소문자 16진수 8자리여야 합니다');
    return override;
  }
  return hex(fnv1a32(seed), 8);
}

export function makeId(tag, kind, dev, step, counter) {
  if (!/^[eba]$/.test(kind)) throw new Error(`알 수 없는 종류 ${kind}`);
  if (dev < 0 || dev > 0xffff) throw new Error('장비 번호 범위 초과');
  if (step < 0 || step > 0xff) throw new Error('단계 번호 범위 초과');
  return `${tag}-${hex(dev, 4)}-4${kind}${hex(step, 2)}-8000-${hex(counter, 12)}`;
}

const UUID_RX = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export const isUuid = (s) => UUID_RX.test(s);

// ---- 부하 프로필 -----------------------------------------------------------------------------------
// 단계: {name, seconds, rate}  rate = 전체 도착률(장/초, 모든 장비 합). 장비마다 rate/ndev 를 보낸다.
// mult 는 C 의 배수. C 는 Phase 1 재측정으로 정한 값(-e C=...). 같은 C 로 양쪽을 돌려야 같은 절대 도착률이 된다.
export const MIN = 60;

export function buildProfile(name, opts) {
  const { C, scale = 1, wRate, wSeconds, burst } = opts;
  const sec = (s) => (scale >= 1 ? s : Math.max(5, Math.round(s * scale)));
  const need = () => { if (!(C > 0)) throw new Error(`${name} 에는 -e C=<장/초> 가 필요합니다`); };
  const stage = (label, mult, seconds) => ({ name: label, seconds: sec(seconds), rate: C * mult, mult });
  switch (name) {
    case 'S0': return [];
    case 'S1': { // Phase 1 의 S1 과 같은 도착률 프로필
      need();
      return [0.2, 0.5, 0.8, 1.0, 1.2, 1.5].map((m) => stage(`${m.toFixed(1)}C`, m, 3 * MIN));
    }
    case 'W': { // 포화: 고정 도착률을 길게(Worker 수를 바꿔 가며 처리량 비교). wRate 는 C 의 배수
      need();
      return [stage(`W${(wRate || 1.5).toFixed(1)}C`, wRate || 1.5, wSeconds || 6 * MIN)];
    }
    case 'D': { // 1.5C 60초 뒤 중단, 큐가 비는 시간 관찰
      need();
      return [stage('1.5C', 1.5, 60), { name: 'drain', seconds: sec(wSeconds || 4 * MIN), rate: 0, mult: 0 }];
    }
    case 'K': { // KEDA 반응: 낮게 → 높게 → 낮게
      need();
      return [stage('0.2C', 0.2, 2 * MIN), stage('1.0C', 1.0, 5 * MIN), stage('0.2C-tail', 0.2, 4 * MIN),
        { name: 'drain', seconds: sec(5 * MIN), rate: 0, mult: 0 }];
    }
    case 'C': { // 콜드: 유휴 뒤 한꺼번에(장비마다 burst 장을 순서대로). 길이는 응답이 오는 만큼 걸려서 rate 로 정하지 않는다.
      return [{ name: 'burst', seconds: sec(60), rate: 0, mult: 0, burst: burst || 6 }];
    }
    case 'SMOKE': // 로컬/스모크: 짧고 작게
      return [{ name: 'smoke', seconds: sec(wSeconds || 20), rate: wRate || 2, mult: 0 }];
    default:
      throw new Error(`알 수 없는 P2_SCENARIO=${name} (S0, S1, W, D, K, C, SMOKE)`);
  }
}

// STEPS 환경변수로 직접 지정: "name:rate:seconds,name:rate:seconds" (rate 는 장/초 절대값)
export function parseSteps(text, scale = 1) {
  return text.split(',').map((part) => {
    const [name, rate, seconds] = part.split(':');
    if (!name || isNaN(Number(rate)) || isNaN(Number(seconds))) throw new Error(`STEPS 형식 오류: ${part}`);
    return { name, rate: Number(rate), seconds: scale >= 1 ? Number(seconds) : Math.max(5, Math.round(Number(seconds) * scale)), mult: 0 };
  });
}

export function withBounds(steps) {
  let at = 0;
  return steps.map((s, i) => { const o = { ...s, index: i, start: at, end: at + s.seconds }; at = o.end; return o; });
}

export const totalSeconds = (steps) => steps.reduce((a, s) => a + s.seconds, 0);

export function stepIndexAt(steps, sec) {
  for (const s of steps) if (sec >= s.start && sec < s.end) return s.index;
  return -1;
}

// ---- 장비별 전송 시각 ------------------------------------------------------------------------------
// 장비 d (0..ndev-1)의 계획된 전송 시각(t0 기준 초) 목록. 장비마다 간격이 같고 시작을 d/ndev 간격만큼 어긋나게 해서
// 모든 장비가 같은 순간에 보내지 않게 한다(실제 장비도 서로 동기화되어 있지 않다).
export function deviceSlots(steps, ndev, dev) {
  const out = [];
  for (const s of steps) {
    if (s.burst) {
      // burst 단계: 단계 시작 시각에 장비마다 burst 장을 "연속으로"(한 장 응답 뒤 바로 다음 장). 시각은 시작과 같다.
      for (let k = 0; k < s.burst; k += 1) out.push({ t: s.start, step: s.index, burst: true });
      continue;
    }
    const perDev = s.rate / ndev;
    if (!(perDev > 0)) continue;
    const interval = 1 / perDev;
    for (let t = s.start + (dev / ndev) * interval; t < s.end - 1e-9; t += interval) out.push({ t, step: s.index, burst: false });
  }
  return out;
}

// ---- 응답 판정 --------------------------------------------------------------------------------------
// 접수 응답을 분류한다: accepted(202), duplicate(200 + body.duplicate), 4xx, 5xx, other(연결 실패·시간 초과 등 status 0).
export function classify(status, body) {
  if (status === 202) return 'accepted';
  if (status === 200) return body && body.duplicate ? 'duplicate' : 'ok200';
  if (status >= 400 && status < 500) return 'http4xx';
  if (status >= 500) return 'http5xx';
  return 'other';
}
