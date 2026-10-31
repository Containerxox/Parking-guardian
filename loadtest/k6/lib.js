// Shared helpers for the Phase 1 load tests (inference.js, scenarios.js).
// Plan and run order: loadtest/README.md. Pre-registration: docs/measurements/phase1/README.md.
//
// Nothing secret is stored here. Credentials and device keys come from the environment (-e NAME=value).
export const env = (name, fallback) => {
  const value = __ENV[name];
  return value === undefined || value === '' ? fallback : value;
};

export const num = (name, fallback) => Number(env(name, fallback));

export const required = (name) => {
  const value = env(name, '');
  if (!value) {
    throw new Error(`환경변수 ${name} 가 필요합니다. 예: k6 run -e ${name}=... (README 의 실행 예 참고)`);
  }
  return value;
};

// Photos are read once in the init stage. IMG_DIR lets the in-cluster Job mount them flat (ConfigMap).
const IMG_DIR = env('IMG_DIR', '../../backend/uploads');
export const PHOTOS = {
  sample_a: open(`${IMG_DIR}/sample_a.jpg`, 'b'), // permitted (sticker present)
  sample_b: open(`${IMG_DIR}/sample_b.jpg`, 'b'), // violation (no sticker)
};

// Which step of the run an event belongs to, from the elapsed time. `steps` = [{name, start, end}] in seconds.
export function stepAt(steps, elapsedMs) {
  const sec = elapsedMs / 1000;
  for (const step of steps) {
    if (sec >= step.start && sec < step.end) return step.name;
  }
  return 'after';
}

// Builds steps from [{name, seconds}], back to back from `offset`.
export function buildSteps(parts, offset = 0) {
  let at = offset;
  return parts.map((part) => {
    const step = { name: part.name, start: at, end: at + part.seconds };
    at = step.end;
    return step;
  });
}

// 로컬 검증용: SCALE=0.05 처럼 주면 모든 단계 길이를 그 비율로 줄인다(최소 5초). 실제 측정에서는 1(기본)이다.
export const SCALE = num('SCALE', 1);
export const scaled = (seconds) => (SCALE >= 1 ? seconds : Math.max(5, Math.round(seconds * SCALE)));
