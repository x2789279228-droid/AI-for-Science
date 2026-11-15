import Link from "next/link";
import ThemeIllust from "@/components/ThemeIllust";

/** 海报式 404(riso 版画风;majorelle 花砖工坊;wuxia 雕版匠人+退隐江湖配文)。 */
export default function NotFound() {
  return (
    <div className="page">
      <div className="notfound-poster">
        <ThemeIllust name="press-house" size={190} />
        <div className="notfound-num">404</div>
        <p className="notfound-caption">此页尚未印出</p>
        <p className="kv" style={{ color: "var(--ink-2)" }}>
          你找的页面不在装订册里——也许被归档,或从未存在。
        </p>
        <Link href="/" className="btn">回到文献库 →</Link>
      </div>
    </div>
  );
}
