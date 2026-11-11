import Link from "next/link";
import { RisoIllust } from "@/components/Illustrations";

/** 海报式 404(riso 皮肤为版画风;classic 下同样成立,只是少了油墨味)。 */
export default function NotFound() {
  return (
    <div className="page">
      <div className="notfound-poster">
        <RisoIllust name="press-house" size={190} />
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
