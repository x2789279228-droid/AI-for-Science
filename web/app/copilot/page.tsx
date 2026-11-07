import { Pen } from "@/components/icons";
import Placeholder from "@/components/Placeholder";

export default function CopilotPage() {
  return (
    <Placeholder
      title="写作 Copilot"
      icon={<Pen size={24} />}
      schedule="D10（11-17）"
      score="检索式引用写作（References 白名单机制）+ CSV→ECharts 出图 + 中→英学术翻译。"
    />
  );
}
