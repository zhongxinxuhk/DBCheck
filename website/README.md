# DBPulse 官方网站

官网通过本仓库 gh-pages 分支根目录部署至 https://zhongxinxuhk.github.io/DBCheck/。

## 编辑与构建

在 gh-pages 分支修改 website/assets/site.css、website/assets/site.js 和 website/scripts/build_site.py。

从仓库根目录执行以下命令，生成首页、下载页、更新日志、文档页和发行快照。

```bash
python3 website/scripts/build_site.py --output .
python3 -m http.server 8765
```

浏览 http://localhost:8765/ 验证后，将生成页面、assets 和 website 源文件一并提交至 gh-pages。

网站访问时读取公开 GitHub Releases，自动同步版本、下载资产和更新日志。API 不可用时显示构建时的发行快照，并保留 GitHub Releases 入口。各平台从最近 30 条正式发行记录中选取最近可用的安装包，较早版本会明确标注。

GitHub Pages 的发布源为 gh-pages 分支的根目录。网站源文件采用头部声明的许可证，品牌图片保留仓库原有信息。项目代码与商业授权说明以 main 分支的许可文件为准。
