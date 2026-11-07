import { Judge } from "@/components/icons";
import Placeholder from "@/components/Placeholder";

export default function JudgePage() {
  return (
    <Placeholder
      title="评审模式 Judge Mode"
      icon={<Judge size={24} />}
      schedule="D11（11-18）"
      score="一键按官方用例顺序全流程演示，每步旁白对应赛题条目（用例通过率 5% + 闭环 10%）。"
    />
  );
}
