// 부하 계획의 "정확한 전송 수"를 계산해 출력한다(서버·네트워크 접속 없음). scenarios_p2.js 와 같은 lib_p2.js 의 같은 함수(buildProfile, deviceSlots)를 쓴다.
//   k6 run --quiet -e P2_SCENARIO=S1 -e C=3.107 -e NDEV=3 plan_counts.js
import { env, num } from './lib.js';
import { buildProfile, parseSteps, withBounds, totalSeconds, deviceSlots } from './lib_p2.js';

export const options = { vus: 1, iterations: 1 };
export default function () {
  const ndev = num('NDEV', 3);
  const scale = num('SCALE', 1);
  const steps = withBounds(env('STEPS', '')
    ? parseSteps(env('STEPS', ''), scale)
    : buildProfile(env('P2_SCENARIO', 'S1'), { C: num('C', 3.107), scale, wRate: num('W_RATE', 0) || undefined, wSeconds: num('W_SECONDS', 0) || undefined, burst: num('BURST', 0) || undefined }));
  const perDevice = [];
  const perStep = steps.map(() => 0);
  for (let d = 0; d < ndev; d += 1) {
    const slots = deviceSlots(steps, ndev, d);
    perDevice.push(slots.length);
    slots.forEach((s) => { perStep[s.step] += 1; });
  }
  console.log(`PLAN ${JSON.stringify({ scenario: env('P2_SCENARIO', 'S1'), ndev, seconds: totalSeconds(steps),
    steps: steps.map((s, i) => ({ name: s.name, seconds: s.seconds, rate: s.rate, planned: perStep[i], ideal: Math.round(s.rate * s.seconds * 100) / 100 })),
    per_device: perDevice, total: perDevice.reduce((a, b) => a + b, 0) })}`);
}
