"use client";

/**
 * 全局动态岛·书童小M版：古风卡通人格层。
 * - 台词池：每个事件键一组古风趣味句（progress/success/error/info 各池），
 *   同键不连续重复；真实语义标题降级为次行小字，展开后仍可见（功能不丢）。
 * - 吉祥物小M：发髻+腮红的古风小书生，执行中随呼吸起伏，成功时眯眼笑。
 * - 布局加长（min-width 340px）增强存在感；墨蓝玻璃底+橙描边不变。
 */

import { motion, AnimatePresence } from "framer-motion";
import { useEffect, useState } from "react";
import { useIsland } from "@/lib/island";

interface PhraseSet {
  progress?: string[];
  success?: string[];
  error?: string[];
  info?: string[];
}

/** 台词池：键 = island.emit 的 key;未命中的键走 FALLBACK 按(kind)取池。 */
const PHRASES: Record<string, PhraseSet> = {
  upload: {
    progress: ["书童小M驮着书卷赶路中……", "一卷一卷搬上书架喽~", "抱紧书卷——小M健步如飞！"],
    success: ["书已上架，静候墨香~", "书架又厚了一寸，请君过目~"],
    error: ["这卷书磕了角……小M先记下它"],
  },
  search: {
    progress: ["别催啦别催啦！小M正在翻遍三座书山~", "arXiv、S2、OpenAlex……小M逐一叩问~",
      "书页翻飞！多元检索进行中～"],
    success: ["三座书山翻完啦！请君验收~", "寻得了！且看这份书单~"],
    error: ["书山暂时闭馆……待会儿再来叩门"],
  },
  refs: {
    progress: ["小M逐条誊抄参考文献，一笔一划~", "谱系梳理中——谁引了谁，慢慢理~"],
    success: ["誊抄完毕，一册谱系呈上~"],
    error: ["誊错了半个字……重来重来"],
  },
  librefs: {
    success: ["谱系誊清！含金量已逐条标好~"],
    error: ["谱系暂时取不到……"],
  },
  dl: {
    progress: ["小M正抱书狂奔而来……", "墨迹未干，书卷装订中~"],
    success: ["新书入库！书架又厚了一寸~", "装订完成，请君收卷~"],
    error: ["这本书飞走了……再试一次？"],
  },
  repro: { info: ["小M去翻了翻代码仓库……"] },
  sub: { success: ["订阅已记下，飞鸽随时待命~"] },
  track: {
    progress: ["飞鸽传书去往 arXiv……", "信使小M正在清点新刊~"],
    success: ["飞鸽归巢！新刊已点收~"],
    error: ["飞鸽迷路了……稍后再放一只"],
  },
  rec: {
    progress: ["小M掐指一算，为你挑书……", "画像称重中——相关性、新近、含金量……"],
    success: ["掐算完毕！这几本与你有缘~"],
    error: ["卦象不明……稍后再算"],
  },
  vote: { info: ["小M记下啦——画像又懂你一分~"] },
  summary: {
    progress: ["小M研墨执笔，综述长卷徐徐展开……", "引经据典中——每句都要有出处~"],
    success: ["长卷收笔！论断皆有凭据~"],
    error: ["墨池干了……综述暂歇"],
  },
  survey: {
    progress: ["小M遍览群书，为君作传……", "五节长卷，一节一节写~"],
    success: ["五节成书！Cache 命中便是缘分~"],
    error: ["写岔了一节……卷轴暂收"],
  },
  checks: {
    progress: ["小M翻墙打探墙外最新进展……", "S2 书库问询中~"],
    success: ["墙外消息已带回！"],
    error: ["墙外风大……打探未果"],
  },
  ideas: {
    progress: ["小M挑灯夜读，寻找未垦之地……"],
    success: ["慧眼所至，缝隙已现！"],
    error: ["灯花爆了一下……扫描暂停"],
  },
  "ideas-run": {
    progress: ["小M挑灯夜读，寻找未垦之地……"],
    success: ["慧眼所至，缝隙已现！"],
    error: ["灯花爆了一下……扫描暂停"],
  },
  qa: {
    progress: ["小M正在召集众 Agent 开会……", "Router→Plan→检索→校验，一步不落~",
      "小M翻书声中，答案渐成形~"],
    success: ["问有所答，答有所据！", "论断皆已盖章，请君过目~"],
    error: ["图打了个盹……小M去摇醒它"],
  },
  "trans-page": {
    progress: ["小M悬梁译书，一字一句皆用心……"],
    success: ["本页译毕，对照呈上~"],
    error: ["这句拗口……翻译卡住了"],
  },
  "trans-all": {
    progress: ["整篇译书是大工程，小M逐块推进~", "译到哪儿存到哪儿——中断也不怕~"],
    success: ["全篇译毕！公式原样保留~"],
    error: ["译笔一滞……已完成块已落缓存"],
  },
  trans: { error: ["这句拗口……单块翻译卡住了"] },
  export: {
    progress: ["小M装帧成册中……", "折页、落款、盖印——马上好~"],
    success: ["成书已交付，请君收好~", "装帧完毕！墨香四溢~"],
    error: ["装订机咬手了……导出未成"],
  },
  dedup: { success: ["同族归档，书架井然~"] },
  theme: {
    info: ["换装完毕！请君品鉴~", "布景已换，戏继续唱~"],
  },
  llm: { info: ["小M的状态变啦——详情请看侧栏小圆点~"] },
};

/** 未命中事件键时的兜底台词池。 */
const FALLBACK: Record<string, string[]> = {
  progress: ["莫急莫急，小M正在奋笔疾书~", "屏息凝神——小M施法中……", "卷轴徐徐展开，答案就在路上~"],
  success: ["大功告成！小M交卷啦~", "成了成了！这厢有礼~", "诸事顺遂，请君过目~"],
  error: ["哎呀呀，出了岔子……小M这就去查错", "唔……此路不通，且容小M再试一次"],
  info: ["叮咚！小M快报~", "小M有一句话要说——"],
};

/** 终态「知道了」按钮的趣味变体。 */
const OK_BUTTONS = ["知道啦~", "退下吧~", "有劳小M~", "辛苦啦~"];

/** 同键不连续重复的随机取句。 */
const lastPick = new Map<string, number>();
function pickLine(pool: string[], seedKey: string): string {
  if (!pool.length) return "";
  let i = Math.floor(Math.random() * pool.length);
  const prev = lastPick.get(seedKey);
  if (pool.length > 1 && i === prev) i = (i + 1) % pool.length;
  lastPick.set(seedKey, i);
  return pool[i];
}

/** 古风小书生吉祥物:发髻+腮红;执行中随呼吸起伏(hover/bob 由 CSS 承担),成功时眯眼笑。 */
function Mascot({ mood, busy }: { mood: "busy" | "happy" | "sad" | "idle"; busy: boolean }) {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" aria-hidden
      className={`island-mascot${busy ? " bob" : ""}`}>
      {/* 发髻 */}
      <circle cx="12" cy="4.4" r="2.1" fill="#ff5f3c" stroke="#17161a" strokeWidth="1.1" />
      {/* 脸 */}
      <circle cx="12" cy="13.2" r="8.4" fill="#fdf1de" stroke="#17161a" strokeWidth="1.4" />
      {/* 刘海 */}
      <path d="M4.6 10.6 Q 12 6.6 19.4 10.6" stroke="#17161a" strokeWidth="1.2" fill="none" />
      {mood === "happy" ? (
        <>
          <path d="M7.8 12.6 q1.4 -1.6 2.8 0" stroke="#17161a" strokeWidth="1.3"
            fill="none" strokeLinecap="round" />
          <path d="M13.4 12.6 q1.4 -1.6 2.8 0" stroke="#17161a" strokeWidth="1.3"
            fill="none" strokeLinecap="round" />
        </>
      ) : (
        <>
          <circle cx="9.2" cy="12.9" r="1.05" fill="#17161a" />
          <circle cx="14.8" cy="12.9" r="1.05" fill="#17161a" />
        </>
      )}
      {mood === "sad" ? (
        <path d="M10 16 q2 -1.4 4 0" stroke="#d23a2e" strokeWidth="1.3"
          fill="none" strokeLinecap="round" />
      ) : (
        <path d="M9.8 15.2 q2.2 2 4.4 0" stroke="#17161a" strokeWidth="1.2"
          fill="none" strokeLinecap="round" />
      )}
      {/* 腮红 */}
      <circle cx="7.2" cy="14.8" r="1.3" fill="#ff5f3c" opacity="0.5" />
      <circle cx="16.8" cy="14.8" r="1.3" fill="#ff5f3c" opacity="0.5" />
    </svg>
  );
}

export default function DynamicIsland() {
  const { event, dismiss } = useIsland();
  const [expanded, setExpanded] = useState(false);
  const [flash, setFlash] = useState(false);
  const [phrase, setPhrase] = useState("");
  const [btnLabel, setBtnLabel] = useState("知道啦~");
  const kind = event?.kind;

  // 新事件到达 → 收起展开态
  useEffect(() => { setExpanded(false); }, [event?.id]);

  // 台词抽取:同事件内不重掷(进度更新不闪台词);终态换池重掷;按钮文案随机
  useEffect(() => {
    if (!event) return;
    const set = PHRASES[event.key] ?? {};
    const pool = set[event.kind] ?? FALLBACK[event.kind] ?? [];
    setPhrase(pickLine(pool, `${event.key}:${event.kind}`));
    if (event.kind === "success" || event.kind === "error") {
      setBtnLabel(pickLine(OK_BUTTONS, "ok-btn"));
    }
  }, [event?.id, event?.key, event?.kind]);

  // 终态到达 → 闪光一次
  useEffect(() => {
    if (kind === "success" || kind === "error") {
      setFlash(true);
      const t = window.setTimeout(() => setFlash(false), 750);
      return () => window.clearTimeout(t);
    }
  }, [event?.id, kind]);

  if (!event) return null;
  const done = kind === "success" || kind === "error";
  const mood = kind === "success" ? "happy" : kind === "error" ? "sad"
    : kind === "progress" ? "busy" : "idle";
  const cls = [
    "island",
    kind === "success" ? "success" : "",
    kind === "error" ? "error" : "",
    flash ? "done-flash" : "",
  ].filter(Boolean).join(" ");

  return (
    <AnimatePresence mode="popLayout">
      <motion.div key={event.id} className={cls} layout data-testid="dynamic-island"
        title={done ? "点击关闭" : "点击展开详情"}
        onClick={() => (done ? dismiss() : setExpanded((v) => !v))}
        initial={{ opacity: 0, y: -18, scale: 0.92 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: -12, scale: 0.94 }}
        transition={{ type: "spring", stiffness: 420, damping: 30 }}
        style={{ transformOrigin: "top center" }}>
        <Mascot mood={mood as "busy" | "happy" | "sad" | "idle"} busy={kind === "progress"} />

        <div style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 1 }}>
          <span className="island-phrase">{phrase || event.title}</span>
          {(expanded || done) && event.title && (
            <span className="island-detail">{event.title}</span>
          )}
          {expanded && event.detail && (
            <span className="island-detail">{event.detail}</span>
          )}
        </div>

        {kind === "progress" && (event.indeterminate || event.progress == null ? (
          <span className="island-spin" />
        ) : (
          <span className="island-ring"
            style={{ ["--p" as string]: Math.round(event.progress! * 100) }} />
        ))}
        {kind === "progress" && event.progress != null && (
          <span className="island-bar">
            <i style={{ width: `${Math.round(event.progress * 100)}%` }} />
          </span>
        )}
        {done && (
          <span className="island-ok chip" data-testid="island-ok">{btnLabel}</span>
        )}
      </motion.div>
    </AnimatePresence>
  );
}
