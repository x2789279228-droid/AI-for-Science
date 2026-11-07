import { Spark } from "@/components/icons";
import Placeholder from "@/components/Placeholder";

export default function DiscoverPage() {
  return (
    <Placeholder
      title="检索发现"
      icon={<Spark size={24} />}
      schedule="D9（11-16）"
      score="arXiv 每日追踪 + 标题下钻自动入库 + 引用含金量打分推荐（Function Calling 6%）。"
    />
  );
}
