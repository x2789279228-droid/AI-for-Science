import { Compass } from "@/components/icons";
import Placeholder from "@/components/Placeholder";

export default function GraphPage() {
  return (
    <Placeholder
      title="引用图谱"
      icon={<Compass size={24} />}
      schedule="D7（11-14）"
      score="库内互引建图 + PageRank 基石论文识别 + Louvain 主题社区（图谱创新 10%，算法官方点名）。"
    />
  );
}
