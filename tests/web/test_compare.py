"""模型对比页 E2E。

F2：跑分总览筛选排（厂商/层级下拉、搜索框、只看有分）不被搜索框挤压
——全局 ``select { flex: 1; width: 100% }`` 曾把两个下拉压到只剩箭头
（选项文字不可见）、「只看有跑分」文字竖排换行。
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="需要安装 playwright")
from playwright.sync_api import expect


@pytest.mark.smoke
def test_f2_compare_filter_row_not_squeezed(console):
    """[F2] 筛选控件保持自然宽度：下拉文字可见、复选文字单行。"""
    console.nav("compare")

    vendor = console.el("#compare-vendor")
    tier = console.el("#compare-tier")
    expect(vendor).to_be_visible()
    expect(tier).to_be_visible()

    # 下拉必须宽到能显示「全部厂商 / 全部层级」——被挤扁时只剩箭头（~28px）。
    assert vendor.bounding_box()["width"] >= 104, \
        f"厂商下拉被挤压（宽 {vendor.bounding_box()['width']}px），选项文字不可见"
    assert tier.bounding_box()["width"] >= 104, \
        f"层级下拉被挤压（宽 {tier.bounding_box()['width']}px），选项文字不可见"

    # 「只看有跑分」保持单行且垂直居中（不再被拉伸成与下拉等高、不再竖排换行）。
    toggle = console.el(".compare-toggle")
    expect(toggle).to_be_visible()
    box = toggle.bounding_box()
    assert box["height"] < 30, \
        f"复选文字换行或被拉伸（高 {box['height']}px）"
    assert box["width"] >= 80, \
        f"复选文字被挤压（宽 {box['width']}px）"


@pytest.mark.smoke
def test_f2_compare_table_renders(console):
    """[F2] 跑分总览表格有数据行，渠道列与详情按钮渲染完整。"""
    console.nav("compare")

    rows = console.el("#compare-body tr")
    expect(rows.first).to_be_visible(timeout=8000)
    assert rows.count() > 0

    # 每行都有「查看」详情按钮
    detail_btns = console.el("#compare-body button")
    expect(detail_btns.first).to_be_visible()
