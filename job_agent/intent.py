from __future__ import annotations

import json
import os
import re

from dotenv import load_dotenv

from .models import SearchIntent


MAJOR_KEYWORDS = {
    "计算机": ["软件工程师", "后端开发", "前端开发", "测试工程师"],
    "软件工程": ["软件工程师", "后端开发", "前端开发", "测试工程师"],
    "人工智能": ["算法工程师", "机器学习工程师", "数据挖掘工程师"],
    "数据科学": ["数据分析师", "数据开发", "数据挖掘工程师"],
    "电子商务": ["电商运营", "产品运营", "用户运营"],
    "市场营销": ["市场专员", "品牌专员", "新媒体运营"],
    "会计": ["会计", "财务助理", "审计助理"],
    "金融": ["金融分析师", "投研助理", "风控专员"],
    "机械": ["机械工程师", "工艺工程师", "设备工程师"],
    "电气": ["电气工程师", "自动化工程师", "嵌入式工程师"],
    "土木": ["土建工程师", "施工员", "造价工程师"],
    "法学": ["法务专员", "合规专员", "律师助理"],
    "人力资源": ["人力资源专员", "招聘专员", "HRBP助理"],
}

CITY_WORDS = [
    "北京",
    "上海",
    "广州",
    "深圳",
    "杭州",
    "成都",
    "南京",
    "武汉",
    "苏州",
    "西安",
    "长沙",
    "重庆",
    "天津",
]


def parse_intent(text: str) -> SearchIntent:
    load_dotenv()
    llm_intent = _parse_with_llm(text)
    if llm_intent:
        return llm_intent
    return _parse_with_rules(text)


def _parse_with_llm(text: str) -> SearchIntent | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL") or None)
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract job search intent as compact JSON with keys: "
                        "keywords(list of Chinese job titles or search terms), city, salary, pages. "
                        "For a major, expand to 2-5 suitable entry-level job titles."
                    ),
                },
                {"role": "user", "content": text},
            ],
        )
        raw = response.choices[0].message.content or ""
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(cleaned)
        keywords = [str(x).strip() for x in data.get("keywords", []) if str(x).strip()]
        if not keywords:
            return None
        return SearchIntent(
            raw_text=text,
            keywords=keywords[:5],
            city=str(data.get("city") or ""),
            salary=str(data.get("salary") or ""),
            pages=max(1, min(int(data.get("pages") or 1), 10)),
        )
    except Exception:
        return None


def _parse_with_rules(text: str) -> SearchIntent:
    city = next((c for c in CITY_WORDS if c in text), "")
    pages = 1
    page_match = re.search(r"(\d+)\s*页", text)
    if page_match:
        pages = max(1, min(int(page_match.group(1)), 10))

    salary = ""
    salary_match = re.search(r"(\d+\s*[-到]\s*\d+\s*[kK]|\d+\s*[kK]\s*以上|面议)", text)
    if salary_match:
        salary = salary_match.group(1).replace(" ", "").upper()

    keywords: list[str] = []
    for major, mapped in MAJOR_KEYWORDS.items():
        if major in text:
            keywords.extend(mapped)
            break

    if not keywords:
        cleaned = text
        for city_word in CITY_WORDS:
            cleaned = cleaned.replace(city_word, "")
        cleaned = re.sub(r"(查询|搜索|爬取|抓取|抓|岗位|职位|相关|对口|专业|工作|第?\d+页|薪资|要求)", " ", cleaned)
        parts = [p.strip() for p in re.split(r"[,，、\s]+", cleaned) if len(p.strip()) >= 2]
        keywords = parts[:3] or [text.strip()]

    return SearchIntent(raw_text=text, keywords=keywords[:5], city=city, salary=salary, pages=pages)
