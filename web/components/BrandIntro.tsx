"use client";

/**
 * 品牌进入动画(kinetic type stage):
 *   幕1 scatter 字母自随机位置+blur 飞入归位 → 幕2 ripple 正弦波扫过
 *   → 幕3 settle 呼吸定格+衬线副标题 → 圆形揭示退出(同时导航 stagger 入场)。
 *
 * 触发规则:
 *   - 每会话首次访问播放(sessionStorage,播放开始即写入)
 *   - `?intro=1` 强制重播(答辩演示)
 *   - 不播:navigator.webdriver(Playwright e2e)/ prefers-reduced-motion / 本会话已播
 *
 * 结构:决策 effect(每实例一次,startedRef 防 StrictMode 双写) + 编排 effect(依赖 phase,
 * StrictMode cleanup 后二次 setup 会重挂全部 WAAPI/timer —— refs 在该 effect 时刻已就绪)。
 * 退场后经 ShellContext.setIntroDone 触发侧边栏 stagger。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useShell } from "./ShellContext";

const WORD = "MERIDIAN";
const T_ACT1 = 760;              // 单字母 scatter 时长
const STAGGER = 60;              // 字母间隔
const T_RIPPLE = 620;
const T_SETTLE = 700;
const T_LEAVE = 520;             // 圆形揭示时长(与 CSS introLeave 对齐)
const PLAY_MS = T_ACT1 + (WORD.length - 1) * STAGGER + T_RIPPLE + T_SETTLE;

export default function BrandIntro() {
  const { setIntroDone } = useShell();
  const [phase, setPhase] = useState<"hidden" | "play" | "leave">("hidden");
  const [risoMode, setRisoMode] = useState(false);       // RISO 皮肤播放分支
  const [stampIn, setStampIn] = useState(false);         // riso 落版盖章
  const wordRef = useRef<HTMLDivElement | null>(null);
  const subRef = useRef<HTMLParagraphElement | null>(null);
  const lineRef = useRef<HTMLElement | null>(null);
  const timers = useRef<number[]>([]);
  const hiddenTimer = useRef<number | null>(null);   // leave→hidden 定时器:独立存放,
  const leaving = useRef(false);                     // 不被编排 effect 的 cleanup 误杀
  const started = useRef(false);

  const finish = useCallback(() => {
    if (leaving.current) return;
    leaving.current = true;
    setPhase("leave");
    hiddenTimer.current = window.setTimeout(() => {
      setPhase("hidden");
      setIntroDone();
    }, T_LEAVE);
  }, [setIntroDone]);

  // 组件卸载时兜底清理 leave 定时器
  useEffect(() => () => {
    if (hiddenTimer.current) window.clearTimeout(hiddenTimer.current);
  }, []);

  // ---- 决策 effect:是否播放(每实例一次) ----
  useEffect(() => {
    const force = new URLSearchParams(window.location.search).get("intro") === "1";
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const seen = sessionStorage.getItem("meridian:intro") === "1";
    if (reduced || navigator.webdriver || (seen && !force)) {
      setIntroDone();
      return;
    }
    if (started.current) return;           // StrictMode 二次 setup:决策已做
    started.current = true;
    try { sessionStorage.setItem("meridian:intro", "1"); } catch { /* 隐私模式忽略 */ }
    try { setRisoMode(localStorage.getItem("meridian:theme") === "riso"); }
    catch { /* 隐私模式忽略 */ }
    setPhase("play");
  }, [setIntroDone]);

  // ---- 编排 effect:phase==="play" 时挂全部动画(StrictMode 下会清理后重挂,幂等) ----
  useEffect(() => {
    if (phase !== "play") return;
    leaving.current = false;
    const anims: Animation[] = [];
    const letters = wordRef.current?.querySelectorAll<HTMLSpanElement>(".intro-letter");

    // 幕1 scatter:随机偏移+blur 飞入
    letters?.forEach((el, i) => {
      const dx = (Math.random() - 0.5) * 240;
      const dy = (Math.random() - 0.5) * 180;
      const rot = (Math.random() - 0.5) * 60;
      anims.push(el.animate([
        { opacity: 0, transform: `translate(${dx}px, ${dy}px) rotate(${rot}deg) scale(1.25)`,
          filter: "blur(12px)" },
        { opacity: 1, transform: "none", filter: "blur(0px)" },
      ], { duration: T_ACT1, delay: i * STAGGER, easing: "cubic-bezier(.22,1,.36,1)", fill: "both" }));
    });

    // 幕2 ripple:波从左向右传播(该字母 scatter 结束后启动,带传播延迟)
    letters?.forEach((el, i) => {
      const delay = T_ACT1 + i * STAGGER + i * 26;
      anims.push(el.animate([
        { transform: "none" },
        { transform: "translateY(-11px)", offset: 0.28 },
        { transform: "none", offset: 0.55 },
        { transform: "translateY(9px)", offset: 0.8 },
        { transform: "none" },
      ], { duration: T_RIPPLE, delay, easing: "ease-in-out", fill: "both" }));
    });

    // 幕3 settle:整体呼吸定格 + 副标题淡入
    const settleStart = T_ACT1 + (WORD.length - 1) * STAGGER + T_RIPPLE;
    if (wordRef.current) {
      anims.push(wordRef.current.animate(
        [{ transform: "scale(1)" }, { transform: "scale(1.045)" }, { transform: "scale(1)" }],
        { duration: T_SETTLE, delay: settleStart, easing: "ease-in-out", fill: "both" }));
    }
    timers.current.push(window.setTimeout(() => subRef.current?.classList.add("in"),
      settleStart - 180));
    if (risoMode) {
      timers.current.push(window.setTimeout(() => setStampIn(true), settleStart + 60));
    }

    // 进度线:rAF 推进到 PLAY_MS
    let raf = 0;
    const t0 = performance.now();
    const tick = () => {
      const p = Math.min(1, (performance.now() - t0) / PLAY_MS);
      if (lineRef.current) lineRef.current.style.width = `${p * 100}%`;
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    timers.current.push(window.setTimeout(finish, PLAY_MS));
    return () => {
      anims.forEach((a) => a.cancel());
      cancelAnimationFrame(raf);
      timers.current.forEach((t) => window.clearTimeout(t));
      timers.current = [];
    };
  }, [phase, finish]);

  // 播放中按 Esc 跳过(点击由 overlay onClick 承担)
  useEffect(() => {
    if (phase !== "play") return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") finish(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [phase, finish]);

  if (phase === "hidden") return null;
  return (
    <div className={`intro-overlay${phase === "leave" ? " leave" : ""}`}
      data-testid="brand-intro" onClick={finish} aria-hidden>
      <div className="intro-word" ref={wordRef}>
        {WORD.split("").map((ch, i) => <span key={i} className="intro-letter">{ch}</span>)}
      </div>
      <p className="intro-sub" ref={subRef}>循证 · 可审计的科研证据系统</p>
      <div className="intro-line"><i ref={lineRef} /></div>
      {risoMode && (
        <span className={`intro-stamp${stampIn ? " in" : ""}`}
          style={{ fontFamily: "var(--font-serif-cn)", fontWeight: 900 }}>证</span>
      )}
      <span className="intro-skip" data-testid="intro-skip">跳过 ›</span>
    </div>
  );
}
