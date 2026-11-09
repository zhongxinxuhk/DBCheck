# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""品牌常量（L1 展示层）。

品牌升级原则：对外展示名 RaccoonX / 浣巡；内部代号 dbcheck 保持不变。
本模块仅承载展示层品牌名，不承载任何内部标识符（包名、命令、路径、变量等）。
"""

# ── 衍生创作署名 / Derivative Work Attribution（追加，不替代上方 fiyo 原始署名）──
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

# 对外英文产品名（展示层，全语言一致，不参与翻译）
PRODUCT_NAME_EN = "DBPulse"
# 对外中文产品名
PRODUCT_NAME_ZH = "数据库智能巡检系统"
# 完整品牌名（中英文组合）
PRODUCT_FULL_NAME = "数据库智能巡检系统（DBPulse）"
# 英文标语（与标题搭配：RaccoonX · Database Inspector）
PRODUCT_TAGLINE_EN = "Intelligent Database Inspection System"

PROJECT_URL = "https://github.com/zhongxinxuhk/DBCheck"
# 更新/公告源：预置的官网地址，不依赖 GitHub 直连（国内网络经常连不上）。
# 后端 modules/web/app.py 定时拉取该地址并缓存，前端只请求本地 /api/announcement。
# 与各作者仓库的关联改由「关于」弹窗里的作者署名链接承载。
SYSTEM_UPDATE_URL = "https://dbcheck.top/announcement.json"
