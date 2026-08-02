import { useMemo } from "react";
function mulberry32(seed: number) {
    return function () {
        seed |= 0;
        seed = (seed + 0x6d2b79f5) | 0;
        let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}
interface Star {
    x: number;
    y: number;
    r: number;
    o: number;
    tw: boolean;
    delay: number;
    dur: number;
}
interface BrightStar {
    x: number;
    y: number;
    r: number;
    o: number;
    delay: number;
    dur: number;
    flare: boolean;
}
export default function Starfield() {
    const layers = useMemo(() => {
        const rng = mulberry32(7);
        const W = 1600;
        const H = 1000;
        const stars: Star[] = [];
        for (let i = 0; i < 520; i++) {
            const r = rng() * 0.7 + 0.25;
            stars.push({
                x: rng() * W,
                y: rng() * H,
                r,
                o: 0.25 + rng() * 0.55,
                tw: rng() < 0.18,
                delay: rng() * 8,
                dur: 4 + rng() * 6,
            });
        }
        const medium: Star[] = [];
        for (let i = 0; i < 90; i++) {
            const r = 0.9 + rng() * 0.7;
            medium.push({
                x: rng() * W,
                y: rng() * H,
                r,
                o: 0.45 + rng() * 0.5,
                tw: rng() < 0.45,
                delay: rng() * 8,
                dur: 5 + rng() * 7,
            });
        }
        const bright: BrightStar[] = [];
        for (let i = 0; i < 18; i++) {
            const r = 1.4 + rng() * 0.9;
            bright.push({
                x: 60 + rng() * (W - 120),
                y: 40 + rng() * (H - 80),
                r,
                o: 0.8 + rng() * 0.2,
                delay: rng() * 8,
                dur: 6 + rng() * 6,
                flare: rng() < 0.5,
            });
        }
        return { stars, medium, bright, W, H };
    }, []);
    return (<div className="guide-starfield" aria-hidden="true">
      <svg viewBox={`0 0 ${layers.W} ${layers.H}`} preserveAspectRatio="xMidYMid slice">
        <defs>
          <radialGradient id="guideStarGlow" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="oklch(0.95 0.05 80)" stopOpacity="0.9"/>
            <stop offset="60%" stopColor="oklch(0.85 0.1 75)" stopOpacity="0.15"/>
            <stop offset="100%" stopColor="oklch(0.85 0.1 75)" stopOpacity="0"/>
          </radialGradient>
          <radialGradient id="guideCenterVignette" cx="50%" cy="35%" r="65%">
            <stop offset="0%" stopColor="oklch(0.25 0.06 250)" stopOpacity="0.35"/>
            <stop offset="100%" stopColor="oklch(0.05 0 0)" stopOpacity="0"/>
          </radialGradient>
        </defs>
        <rect width={layers.W} height={layers.H} fill="url(#guideCenterVignette)"/>
        {layers.stars.map((s, i) => (<circle key={`s${i}`} cx={s.x} cy={s.y} r={s.r} fill="oklch(0.97 0.02 85)" opacity={s.o} className={s.tw ? "guide-tw" : ""} style={s.tw
                ? ({
                    "--o": s.o,
                    "--d": `${s.dur}s`,
                    "--delay": `${s.delay}s`,
                } as React.CSSProperties)
                : undefined}/>))}
        {layers.medium.map((s, i) => (<circle key={`m${i}`} cx={s.x} cy={s.y} r={s.r} fill="oklch(0.98 0.03 80)" opacity={s.o} className={s.tw ? "guide-tw" : ""} style={s.tw
                ? ({
                    "--o": s.o,
                    "--d": `${s.dur}s`,
                    "--delay": `${s.delay}s`,
                } as React.CSSProperties)
                : undefined}/>))}
        {layers.bright.map((s, i) => (<circle key={`b${i}`} cx={s.x} cy={s.y} r={s.r} fill="oklch(0.99 0.04 80)" opacity={s.o} className="guide-tw" style={{
                "--o": s.o,
                "--d": `${s.dur}s`,
                "--delay": `${s.delay}s`,
            } as React.CSSProperties}/>))}
      </svg>
      <div className="guide-starfield-grain"/>
    </div>);
}
