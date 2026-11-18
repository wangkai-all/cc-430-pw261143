"""Registered report (aggregate) definitions.

Every report of a revision batch is recomputed against the SAME pinned input
boundary — no report may go pick "the latest data" on its own.
"""

MEASURES = ("amount", "qty")

REPORTS = {
    "regional_sales": {"dimensions": ["region"], "title": "区域销售报表"},
    "product_sales": {"dimensions": ["product"], "title": "产品销售报表"},
    "channel_sales": {"dimensions": ["channel"], "title": "渠道销售报表"},
    "region_product": {"dimensions": ["region", "product"], "title": "区域×产品交叉报表"},
}
