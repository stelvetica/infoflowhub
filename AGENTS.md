# 项目约定

## bsk 技能名

bsk 内嵌模板写死 `browser-skill`，本机统一用 `develop-browser-skill`。

跑完 `bsk install-skill` / `bsk update` 后：把新出现的 `~/.agents/skills/browser-skill/` 合入 `develop-browser-skill/`，改回 `SKILL.md` 的 `name` 与标题，删除该目录；`~/.zcode/skills/` 副本同步一致。
