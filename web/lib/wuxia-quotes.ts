/**
 * 武侠语录表（v1.4，design-tokens.md §6）：玄墨夜行皮肤专属的艺术层。
 * 经典名句为主 + 出处标注；仅 wuxia 主题经 WuxiaTagline / 空态配文 / 侧栏立言渲染。
 * ROUTE_QUOTES 按路径前缀匹配（usePathname），未命中路由回退 FALLBACK_QUOTE。
 */

export interface WuxiaQuote {
  text: string;    // 名句正文
  from: string;    // 出处
}

/** 路由题跋（竖排挂主区右上，一一对应页面语义）。 */
export const ROUTE_QUOTES: { match: (path: string) => boolean; quote: WuxiaQuote }[] = [
  {
    match: (p) => p === "/",
    quote: { text: "飞雪连天射白鹿，笑书神侠倚碧鸳", from: "金庸书名联" },   // 藏书意象 → 文献库
  },
  {
    match: (p) => p.startsWith("/chat"),
    quote: { text: "小李飞刀，例不虚发", from: "古龙《多情剑客无情剑》" },    // 每论断必有出处 → 溯源问答
  },
  {
    match: (p) => p.startsWith("/reader"),
    quote: { text: "书山有路勤为径", from: "韩愈" },                          // 秘籍在握 → 阅读器
  },
  {
    match: (p) => p.startsWith("/graph"),
    quote: { text: "有人的地方就有江湖", from: "《笑傲江湖》" },              // 引用关系网 → 图谱
  },
  {
    match: (p) => p.startsWith("/discover"),
    quote: { text: "踏破铁鞋无觅处，得来全不费功夫", from: "《水浒传》" },    // 三源检索 → 发现
  },
  {
    match: (p) => p.startsWith("/ideas"),
    quote: { text: "山重水复疑无路，柳暗花明又一村", from: "陆游《游山西村》" }, // 缝隙 → 转机
  },
  {
    match: (p) => p.startsWith("/copilot"),
    quote: { text: "无剑胜有剑", from: "金庸《神雕侠侣》" },                  // 独孤求败 → 下笔如有神
  },
  {
    match: (p) => p.startsWith("/judge"),
    quote: { text: "华山论剑，以证武道", from: "《射雕英雄传》" },            // 评审 → 论剑
  },
  {
    match: (p) => p.startsWith("/audit"),
    quote: { text: "明人不做暗事", from: "《西游记》" },                      // 透明度 → 光明磊落
  },
  {
    match: (p) => p.startsWith("/health"),
    quote: { text: "任督二脉，一旦贯通", from: "武侠内功通说" },              // 健康 → 百脉皆通
  },
  {
    match: (p) => p.startsWith("/diff"),
    quote: { text: "武功版本，高低立判", from: "江湖门派旧闻" },              // 版本对比
  },
];

/** 兜底语录（/diff 之外的未知路由）。 */
export const FALLBACK_QUOTE: WuxiaQuote = {
  text: "人在江湖，身不由己",
  from: "古龙《楚留香传奇》",
};

/** 侧栏立言（站桩铭志，常驻）。 */
export const SIDE_MOTTO: WuxiaQuote = {
  text: "侠之大者，为国为民",
  from: "金庸《神雕侠侣》",
};

/** 404 配文（not-found 海报页）。 */
export const QUOTE_404: WuxiaQuote = {
  text: "已退出江湖，莫再寻它",
  from: "《笑傲江湖》",
};

/** 空态场景配文（与 IllustName 同名场景一一对应，随插画展示）。 */
export const SCENE_QUOTES: Record<string, WuxiaQuote> = {
  "night-reader": { text: "青灯黄卷，夜读兵书", from: "书院夜读" },
  "paper-sea": { text: "一蓑烟雨任平生", from: "苏轼《定风波》" },
  lighthouse: { text: "灯在，路就在", from: "江湖夜话" },
  peaks: { text: "会当凌绝顶，一览众山小", from: "杜甫《望岳》" },
  magnify: { text: "真伪之辨，存乎一心", from: "六扇门验印旧例" },
  observatory: { text: "高手过招，胜负一念间", from: "华山旧事" },
  "press-house": { text: "此页已退出江湖，莫再寻它", from: "《笑傲江湖》" },
  "wavy-flag": { text: "旗在人在，风起旗扬", from: "镖局行规" },
};
