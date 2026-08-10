# 微信公众号抓取迁移：外部工具微信读书 RSS

## 背景

微信于 2026-07-30 关闭了公众号后台文章列表接口 `mp.weixin.qq.com/cgi-bin/appmsgpublish`
（对一切请求统一返回 `200013 freq control`），本仓原微信 connector 因此失效。

本方案改为：**外部工具 `wechat-mp-tools`（微信读书模式）** 负责抓取公众号文章，并输出标准 RSS 2.0；
本仓把 9 个公众号源改为 `native` RSS 源，直接轮询该工具生成的 RSS。本仓**零代码改动**。

> ⚠️ 该工具依赖微信读书的非官方接口，且请求会经第三方转发服务（weread 中转域）。
> 微信读书会话会交给匿名第三方；且**并非所有公众号都被微信读书收录**——未收录的号无法通过此通道抓取。

## 外部工具安装与启动（用户人工）

```bash
git clone https://github.com/x554960766/wechat-mp-tools.git
cd wechat-mp-tools
pip install -r requirements.txt
python app.py          # 或 python main.py（桌面版）
```

- 服务地址：`http://localhost:5200`
- 启动后在界面用**手机微信扫码登录微信读书**。⚠️ 勿勾选"24 小时后自动退出"。

## 添加公众号

1. 找到该公众号任一篇文章链接（`https://mp.weixin.qq.com/s/...`）。
2. 在工具里粘贴链接，自动反查并添加该公众号。
3. 工具会在后台按微信读书协议拉取该号文章并生成 RSS。

> 3 个仅含 `wechat://mp/<fakeid>` 的源（finn的投研记录、沧海一土狗、180K、贝叶斯之美、
> 表舅是养基大户、精选君）在旧 config 里没有文章链接，需要你补任一篇该号的文章链接才能添加。

## 本仓 9 个源对应的 RSS 地址

工具 RSS 按**公众号真实昵称**匹配（`/api/articles/rss/<昵称>`）。config 里已按"去分类前缀"的
名字配置，若与微信读书里的真实昵称不一致，需修正 `config/subscription_sources.json` 里
对应源的 `feed_url` 中的昵称部分。

| 源 id | 配置的 feed 昵称（去前缀） |
|---|---|
| wechat-finninvestrecord | finn的投研记录 |
| wechat-canghaiyitugou | 沧海一土狗 |
| wechat-180k | 180K |
| wechat-bayes-beauty | 贝叶斯之美 |
| wechat-bigmoneyfundsuncle | 表舅是养基大户 |
| wechat-pangtegong | 胖特工 |
| wechat-jingxuanjun | 精选君 |
| wechat-shuimujiyao | 水木纪要 |
| wechat-nasdaq-new-high | 今天纳指新高了吗 |

## 验证

工具运行且 9 个号都已添加后，在本仓执行"立即抓取"，确认 9 个源成功入库。
未收录 / 抓取失败的源会如实报错，不回退到已失效的微信 connector。

## 需要人工确认的点

- 每个号是否被微信读书收录（未收录无法抓取）。
- 每个号在微信读书的真实昵称（决定 RSS 地址是否正确）。
