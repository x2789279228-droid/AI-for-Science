# 种子数据 20% 人工抽检清单（半人工金标）

> 核对每条内容与其 kind/conversation 是否自洽；核对完成后将 gold meta 的
> human_review.status 置为 passed 并签署。

- [ ] (1) [hypothesis] 我假设稀疏注意力能把长文本推理的显存占用降低 40%  （seed_c02）
- [ ] (5) [hypothesis] 我假设混合检索能把召回率提升 15 个百分点  （seed_c05）
- [ ] (8) [hypothesis] 我假设课程学习能加速小模型收敛  （seed_c07）
- [ ] (9) [experiment_result] 实验结果表明稀疏注意力显存占用下降 38%，与假设一致  （seed_c08）
- [ ] (10) [hypothesis] 我假设批处理管道是数据接入的最优解  （seed_c09）
- [ ] (13) [experiment_result] 复现 5 组长文本实验均显示稀疏注意力能明显降低显存占用  （seed_c12）
- [ ] (15) [experiment_result] 实验证实混合检索召回率提升 16 个百分点，支持该假设  （seed_c14）
- [ ] (23) [hypothesis] 我修正假设：流式管道取代批处理管道成为数据接入最优解  （seed_c20）
- [ ] (28) [experiment_result] 复现实验推翻了稀疏注意力降低 40% 显存的假设：实测仅 12%，且长序列下反而升高  （seed_c24）
