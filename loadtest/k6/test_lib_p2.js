// lib_p2.js 의 순수 함수 시험. 실행: k6 run --vus 1 --iterations 1 test_lib_p2.js   (서버·AWS 접속 없음)
import { check } from 'k6';
import { runTag, makeId, isUuid, buildProfile, parseSteps, withBounds, totalSeconds, stepIndexAt, deviceSlots, classify, fnv1a32 } from './lib_p2.js';

export const options = { thresholds: { checks: ['rate==1'] }, vus: 1, iterations: 1 };

// k6 는 반복 안의 예외로는 종료 코드를 바꾸지 않는다(0 으로 끝난다). 그래서 check 로 기록하고 임계값(checks rate==1)으로 실패 코드를 만든다.
function assert(cond, msg) { check(null, { [msg]: () => cond }); if (!cond) throw new Error(`FAIL: ${msg}`); }
const near = (a, b, tol) => Math.abs(a - b) <= tol;

export default function () {
  // ---- 식별자: UUID 형식, 유일성, 실행(run tag) 분리
  const tag = runTag('abc|1');
  assert(/^[0-9a-f]{8}$/.test(tag), 'run tag 8 hex');
  assert(runTag('abc|1') === tag && runTag('abc|2') !== tag, 'run tag 는 seed 에 따라 결정적');
  assert(runTag('x', 'deadbeef') === 'deadbeef', 'RUN_TAG override');
  let threw = false; try { runTag('x', 'ZZ'); } catch (e) { threw = true; } assert(threw, '잘못된 RUN_TAG 는 거부');
  const seen = new Set();
  for (let dev = 0; dev < 3; dev += 1) for (let step = 0; step < 6; step += 1) for (let n = 0; n < 700; n += 1) {
    const id = makeId(tag, 'e', dev, step, dev * 1e6 + step * 1e4 + n);
    assert(isUuid(id), `UUID 형식 ${id}`);
    assert(!seen.has(id), `중복 id ${id}`);
    seen.add(id);
  }
  assert(seen.size === 3 * 6 * 700, '12,600 개 id 모두 유일');
  assert(makeId(tag, 'e', 0, 0, 0) !== makeId(tag, 'b', 0, 0, 0) && makeId(tag, 'e', 0, 0, 0) !== makeId(tag, 'a', 0, 0, 0), '종류별 id 분리');
  assert(makeId(tag, 'e', 1, 2, 3).startsWith(tag), 'id 는 run tag 로 시작(DB 에서 실행별 필터)');
  assert(makeId(tag, 'e', 1, 2, 3).split('-')[2] === '4e02', '단계 번호가 id 에 들어간다');
  threw = false; try { makeId(tag, 'e', 0, 256, 0); } catch (e) { threw = true; } assert(threw, '단계 범위 초과 거부');
  assert(fnv1a32('') === 0x811c9dc5, 'FNV 초기값');

  // ---- 프로필
  const C = 3.107;
  const s1 = withBounds(buildProfile('S1', { C }));
  assert(s1.length === 6 && totalSeconds(s1) === 6 * 180, 'S1: 6 단계 × 180 s');
  assert(['0.2C', '0.5C', '0.8C', '1.0C', '1.2C', '1.5C'].join() === s1.map((s) => s.name).join(), 'S1 단계 이름');
  assert(near(s1[5].rate, C * 1.5, 1e-9), '1.5C 도착률');
  assert(s1[0].start === 0 && s1[5].end === 1080, '단계 경계');
  assert(stepIndexAt(s1, 0) === 0 && stepIndexAt(s1, 179.9) === 0 && stepIndexAt(s1, 180) === 1 && stepIndexAt(s1, 1080) === -1, 'stepIndexAt');
  assert(buildProfile('S0', {}).length === 0, 'S0 은 사진 없음');
  threw = false; try { buildProfile('S1', {}); } catch (e) { threw = true; } assert(threw, 'C 없이 S1 거부');
  const d = withBounds(buildProfile('D', { C }));
  assert(d[0].seconds === 60 && d[1].rate === 0, 'D: 60 s 부하 + 소화');
  const k = withBounds(buildProfile('K', { C }));
  assert(k.length === 4 && k[1].rate > k[0].rate && k[3].rate === 0, 'K 모양');
  const scaledS1 = buildProfile('S1', { C, scale: 0.01 });
  assert(scaledS1.every((s) => s.seconds === 5), 'SCALE 은 최소 5 s');
  const manual = parseSteps('a:2:10,b:0:5');
  assert(manual.length === 2 && manual[0].rate === 2 && manual[1].seconds === 5, 'STEPS 파싱');
  threw = false; try { parseSteps('broken'); } catch (e) { threw = true; } assert(threw, 'STEPS 형식 오류 거부');

  // ---- 장비별 전송 시각: 간격, 개수, 어긋남, 합계
  for (const ndev of [1, 3]) {
    const perStepTotal = new Array(s1.length).fill(0);
    for (let dev = 0; dev < ndev; dev += 1) {
      const slots = deviceSlots(s1, ndev, dev);
      for (let i = 1; i < slots.length; i += 1) assert(slots[i].t > slots[i - 1].t, '장비별 전송 시각은 엄격히 증가');
      slots.forEach((sl) => { perStepTotal[sl.step] += 1; assert(sl.t >= s1[sl.step].start && sl.t < s1[sl.step].end, '슬롯은 자기 단계 안'); });
    }
    s1.forEach((s, i) => assert(near(perStepTotal[i], s.rate * s.seconds, ndev), `단계 ${s.name} 총 전송 수 ${perStepTotal[i]} ≈ ${(s.rate * s.seconds).toFixed(1)} (±${ndev})`));
  }
  const a = deviceSlots(s1, 3, 0)[0].t; const b = deviceSlots(s1, 3, 1)[0].t; const c = deviceSlots(s1, 3, 2)[0].t;
  assert(a < b && b < c && b - a > 0 && near(b - a, c - b, 1e-9), '장비 시작은 서로 어긋난다');
  const total1 = s1.reduce((acc, s) => acc + s.rate * s.seconds, 0);
  assert(near(total1, 5.2 * C * 180, 1e-6), 'S1 사진 총수 = 5.2 × C × 180 (≈2,908 장)');
  const burst = withBounds(buildProfile('C', { burst: 4 }));
  const bs = deviceSlots(burst, 3, 1);
  assert(bs.length === 4 && bs.every((x) => x.burst && x.t === 0), 'C: 장비마다 burst 장, 시작 시각 동일');
  assert(deviceSlots(withBounds(buildProfile('D', { C })), 3, 0).filter((x) => x.step === 1).length === 0, '소화 단계에는 전송 없음');

  // 스모크 10장: STEPS=smoke10:1:10 -> 장비별 4/3/3, 장비 간격 3 s, 시작은 1 s 씩 어긋남
  const sm = withBounds(parseSteps('smoke10:1:10'));
  const counts = [0, 1, 2].map((dv) => deviceSlots(sm, 3, dv));
  assert(counts.map((c) => c.length).join() === '4,3,3', '스모크 10장은 장비별 4/3/3');
  assert(counts[0].map((x) => x.t).join() === '0,3,6,9' && counts[1].map((x) => x.t).join() === '1,4,7' && counts[2].map((x) => x.t).join() === '2,5,8', '스모크 전송 시각');

  // ---- 응답 분류
  assert(classify(202, { job_id: 'x' }) === 'accepted', '202');
  assert(classify(200, { duplicate: true }) === 'duplicate' && classify(200, { duplicate: false }) === 'ok200', '200 duplicate');
  assert(classify(409, null) === 'http4xx' && classify(404, null) === 'http4xx', '4xx');
  assert(classify(503, null) === 'http5xx' && classify(502, null) === 'http5xx', '5xx');
  assert(classify(0, null) === 'other', 'status 0');
}
