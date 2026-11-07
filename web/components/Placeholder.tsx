import Link from "next/link";
import { ChevronRight } from "./icons";

interface PlaceholderProps {
  title: string;
  icon: React.ReactNode;
  schedule: string;
  score: string; // 对应参赛评分点
}

/** 未实现模块的统一占位页（排期可见，蓝图完整）。 */
export default function Placeholder({ title, icon, schedule, score }: PlaceholderProps) {
  return (
    <div className="placeholder">
      <div className="placeholder-card">
        <div className="placeholder-icon">{icon}</div>
        <h2>{title}</h2>
        <p>
          该模块按参赛方案排期于 <b>{schedule}</b> 交付。{score}
        </p>
        <Link href="/" className="btn ghost" style={{ display: "inline-flex" }}>
          返回文献库 <ChevronRight size={14} />
        </Link>
      </div>
    </div>
  );
}
