"""管理端数据底座（零外部依赖）：SQLite (WAL) 用户体系、RBAC 权限、会话、操作审计日志与中文姓名转全拼。

持久化位置：`${output_dir}/state/platform.db`（权限 0600）。
"""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from . import vcs
except ImportError:
    import vcs  # type: ignore[no-redef]

ROLES = ("superadmin", "admin", "user")
EMAIL_NOTIFY_MODES = ("batch", "risk_only", "off")
ISSUE_TRIAGE_STATUSES = ("open", "planned", "fixed", "exempted")
SESSION_TTL_SEC = 24 * 3600
PROMPT_KEYS = ("skill_md", "dispatch_extra", "summary_prompt")
PROMPT_FILENAMES = {
    "skill_md": "SKILL.md",
    "dispatch_extra": "dispatch_extra.txt",
    "summary_prompt": "summary_prompt.txt",
}
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,64}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VALID_PROJECT_STATUSES = ("在研", "停用", "归档")
PROJECT_CSV_FIELDS = (
    "project_id",
    "project_name",
    "department",
    "owner",
    "vcs_type",
    "git_url",
    "branch",
    "status",
    "tech_stack",
    "project_type",
    "core_flows",
    "scope_out",
    "materials_dir",
    "weights",
    "notes",
)
_EXT_LANG_MAP: dict[str, str] = {
    ".java": "Java", ".kt": "Kotlin", ".scala": "Scala", ".groovy": "Groovy",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
    ".vue": "Vue", ".py": "Python", ".go": "Go", ".rs": "Rust",
    ".c": "C", ".h": "C/C++", ".cpp": "C++", ".cc": "C++", ".hpp": "C++",
    ".cs": "C#", ".php": "PHP", ".rb": "Ruby", ".swift": "Swift", ".dart": "Dart",
    ".sql": "SQL", ".sh": "Shell", ".bash": "Shell",
    ".html": "HTML", ".htm": "HTML", ".css": "CSS", ".scss": "SCSS", ".less": "Less",
    ".xml": "XML", ".yml": "YAML", ".yaml": "YAML", ".json": "JSON",
}
_SKIP_DIR_PARTS = {"node_modules", "dist", "build", "target", "vendor", ".git", ".svn", ".idea", ".vscode", "__pycache__", "out"}

# GB2312 一级汉字（3755 个常用字）按拼音顺序编码的边界码表（GBK 两字节整数 -> 拼音）
_GB2312_PINYIN_BOUNDARIES: tuple[tuple[int, str], ...] = (
    (0xB0A1, "a"), (0xB0A3, "ai"), (0xB0B0, "an"), (0xB0B9, "ang"), (0xB0BC, "ao"),
    (0xB0C5, "ba"), (0xB0D7, "bai"), (0xB0DF, "ban"), (0xB0EE, "bang"), (0xB0FA, "bao"),
    (0xB1AD, "bei"), (0xB1BC, "ben"), (0xB1C0, "beng"), (0xB1C6, "bi"), (0xB1DE, "bian"),
    (0xB1EA, "biao"), (0xB1EE, "bie"), (0xB1F2, "bin"), (0xB1F8, "bing"), (0xB2A3, "bo"),
    (0xB2B8, "bu"), (0xB2C1, "ca"), (0xB2C2, "cai"), (0xB2CD, "can"), (0xB2D4, "cang"),
    (0xB2D9, "cao"), (0xB2DE, "ce"), (0xB2E3, "ceng"), (0xB2E5, "cha"), (0xB2F0, "chai"),
    (0xB2F3, "chan"), (0xB2FD, "chang"), (0xB3AC, "chao"), (0xB3B5, "che"), (0xB3BB, "chen"),
    (0xB3C5, "cheng"), (0xB3D4, "chi"), (0xB3E4, "chong"), (0xB3E9, "chou"), (0xB3F5, "chu"),
    (0xB4A7, "chuai"), (0xB4A8, "chuan"), (0xB4AF, "chuang"), (0xB4B5, "chui"), (0xB4BA, "chun"),
    (0xB4C1, "chuo"), (0xB4C3, "ci"), (0xB4CF, "cong"), (0xB4D5, "cou"), (0xB4D6, "cu"),
    (0xB4DA, "cuan"), (0xB4DD, "cui"), (0xB4E5, "cun"), (0xB4E8, "cuo"), (0xB4EE, "da"),
    (0xB4F4, "dai"), (0xB5A2, "dan"), (0xB5B1, "dang"), (0xB5B6, "dao"), (0xB5C2, "de"),
    (0xB5C5, "deng"), (0xB5CC, "di"), (0xB5DF, "dian"), (0xB5EF, "diao"), (0xB5F8, "die"),
    (0xB6A1, "ding"), (0xB6AA, "diu"), (0xB6AB, "dong"), (0xB6B5, "dou"), (0xB6BC, "du"),
    (0xB6CB, "duan"), (0xB6D1, "dui"), (0xB6D5, "dun"), (0xB6DE, "duo"), (0xB6EA, "e"),
    (0xB6F7, "en"), (0xB6F8, "er"), (0xB7A2, "fa"), (0xB7AA, "fan"), (0xB7BB, "fang"),
    (0xB7C6, "fei"), (0xB7D2, "fen"), (0xB7E1, "feng"), (0xB7F0, "fo"), (0xB7F1, "fou"),
    (0xB7F2, "fu"), (0xB8C1, "ga"), (0xB8C3, "gai"), (0xB8C9, "gan"), (0xB8D4, "gang"),
    (0xB8DD, "gao"), (0xB8E7, "ge"), (0xB8F8, "gei"), (0xB8F9, "gen"), (0xB8FB, "geng"),
    (0xB9A4, "gong"), (0xB9B3, "gou"), (0xB9BC, "gu"), (0xB9CE, "gua"), (0xB9D4, "guai"),
    (0xB9D7, "guan"), (0xB9E2, "guang"), (0xB9E5, "gui"), (0xB9F5, "gun"), (0xB9F8, "guo"),
    (0xB9FE, "ha"), (0xBAA1, "hai"), (0xBAA8, "han"), (0xBABB, "hang"), (0xBABE, "hao"),
    (0xBAC7, "he"), (0xBAD9, "hei"), (0xBADB, "hen"), (0xBADF, "heng"), (0xBAE4, "hong"),
    (0xBAED, "hou"), (0xBAF4, "hu"), (0xBBA8, "hua"), (0xBBB1, "huai"), (0xBBB6, "huan"),
    (0xBBC4, "huang"), (0xBBD2, "hui"), (0xBBE7, "hun"), (0xBBED, "huo"), (0xBBF7, "ji"),
    (0xBCCE, "jia"), (0xBCDF, "jian"), (0xBDA9, "jiang"), (0xBDB6, "jiao"), (0xBDD2, "jie"),
    (0xBDED, "jin"), (0xBEA3, "jing"), (0xBEBC, "jiong"), (0xBEBE, "jiu"), (0xBECF, "ju"),
    (0xBEE8, "juan"), (0xBEEF, "jue"), (0xBEF9, "jun"), (0xBFA6, "ka"), (0xBFAA, "kai"),
    (0xBFAF, "kan"), (0xBFB5, "kang"), (0xBFBC, "kao"), (0xBFC0, "ke"), (0xBFCF, "ken"),
    (0xBFD3, "keng"), (0xBFD5, "kong"), (0xBFD9, "kou"), (0xBFDD, "ku"), (0xBFE4, "kua"),
    (0xBFE9, "kuai"), (0xBFED, "kuan"), (0xBFEF, "kuang"), (0xBFF7, "kui"), (0xC0A4, "kun"),
    (0xC0A8, "kuo"), (0xC0AC, "la"), (0xC0B3, "lai"), (0xC0B6, "lan"), (0xC0C5, "lang"),
    (0xC0CC, "lao"), (0xC0D5, "le"), (0xC0D7, "lei"), (0xC0E2, "leng"), (0xC0E5, "li"),
    (0xC1A9, "lia"), (0xC1AA, "lian"), (0xC1B8, "liang"), (0xC1C3, "liao"), (0xC1D0, "lie"),
    (0xC1D5, "lin"), (0xC1E1, "ling"), (0xC1EF, "liu"), (0xC1FA, "long"), (0xC2A5, "lou"),
    (0xC2AB, "lu"), (0xC2BF, "lv"), (0xC2CD, "luan"), (0xC2D3, "lue"), (0xC2D5, "lun"),
    (0xC2DC, "luo"), (0xC2E8, "ma"), (0xC2F1, "mai"), (0xC2F7, "man"), (0xC3A2, "mang"),
    (0xC3A8, "mao"), (0xC3B4, "me"), (0xC3B5, "mei"), (0xC3C5, "men"), (0xC3C8, "meng"),
    (0xC3D0, "mi"), (0xC3DE, "mian"), (0xC3E7, "miao"), (0xC3EF, "mie"), (0xC3F1, "min"),
    (0xC3F7, "ming"), (0xC3FD, "miu"), (0xC3FE, "mo"), (0xC4B1, "mou"), (0xC4B4, "mu"),
    (0xC4C3, "na"), (0xC4CA, "nai"), (0xC4CF, "nan"), (0xC4D2, "nang"), (0xC4D3, "nao"),
    (0xC4D8, "ne"), (0xC4D9, "nei"), (0xC4DB, "nen"), (0xC4DC, "neng"), (0xC4DD, "ni"),
    (0xC4E8, "nian"), (0xC4EF, "niang"), (0xC4F1, "niao"), (0xC4F3, "nie"), (0xC4FA, "nin"),
    (0xC4FB, "ning"), (0xC5A3, "niu"), (0xC5A7, "nong"), (0xC5AB, "nu"), (0xC5AE, "nv"),
    (0xC5AF, "nuan"), (0xC5B0, "nue"), (0xC5B2, "nuo"), (0xC5B6, "o"), (0xC5B7, "ou"),
    (0xC5BE, "pa"), (0xC5C4, "pai"), (0xC5CA, "pan"), (0xC5D2, "pang"), (0xC5D7, "pao"),
    (0xC5DE, "pei"), (0xC5E7, "pen"), (0xC5E9, "peng"), (0xC5F7, "pi"), (0xC6AA, "pian"),
    (0xC6AE, "piao"), (0xC6B2, "pie"), (0xC6B4, "pin"), (0xC6B9, "ping"), (0xC6C2, "po"),
    (0xC6CB, "pu"), (0xC6DA, "qi"), (0xC6FE, "qia"), (0xC7A3, "qian"), (0xC7B9, "qiang"),
    (0xC7C1, "qiao"), (0xC7D0, "qie"), (0xC7D5, "qin"), (0xC7E0, "qing"), (0xC7ED, "qiong"),
    (0xC7EF, "qiu"), (0xC7F7, "qu"), (0xC8A6, "quan"), (0xC8B1, "que"), (0xC8B9, "qun"),
    (0xC8BB, "ran"), (0xC8BF, "rang"), (0xC8C4, "rao"), (0xC8C7, "re"), (0xC8C9, "ren"),
    (0xC8D3, "reng"), (0xC8D5, "ri"), (0xC8D6, "rong"), (0xC8E0, "rou"), (0xC8E3, "ru"),
    (0xC8ED, "ruan"), (0xC8EF, "rui"), (0xC8F2, "run"), (0xC8F4, "ruo"), (0xC8F6, "sa"),
    (0xC8F9, "sai"), (0xC8FD, "san"), (0xC9A3, "sang"), (0xC9A6, "sao"), (0xC9AA, "se"),
    (0xC9AD, "sen"), (0xC9AE, "seng"), (0xC9AF, "sha"), (0xC9B8, "shai"), (0xC9BA, "shan"),
    (0xC9CA, "shang"), (0xC9D2, "shao"), (0xC9DD, "she"), (0xC9E9, "shen"), (0xC9F9, "sheng"),
    (0xCAA6, "shi"), (0xCAD5, "shou"), (0xCADF, "shu"), (0xCBA2, "shua"), (0xCBA4, "shuai"),
    (0xCBA8, "shuan"), (0xCBAA, "shuang"), (0xCBAD, "shui"), (0xCBB1, "shun"), (0xCBB5, "shuo"),
    (0xCBB9, "si"), (0xCBC9, "song"), (0xCBD1, "sou"), (0xCBD4, "su"), (0xCBE1, "suan"),
    (0xCBE4, "sui"), (0xCBEF, "sun"), (0xCBF2, "suo"), (0xCBFA, "ta"), (0xCCA5, "tai"),
    (0xCCAE, "tan"), (0xCCC0, "tang"), (0xCCCD, "tao"), (0xCCD8, "te"), (0xCCD9, "teng"),
    (0xCCDD, "ti"), (0xCCEC, "tian"), (0xCCF4, "tiao"), (0xCCF9, "tie"), (0xCCFC, "ting"),
    (0xCDA8, "tong"), (0xCDB5, "tou"), (0xCDB9, "tu"), (0xCDC4, "tuan"), (0xCDC6, "tui"),
    (0xCDCC, "tun"), (0xCDCF, "tuo"), (0xCDDA, "wa"), (0xCDE1, "wai"), (0xCDE3, "wan"),
    (0xCDF4, "wang"), (0xCDFE, "wei"), (0xCEC1, "wen"), (0xCECB, "weng"), (0xCECE, "wo"),
    (0xCED7, "wu"), (0xCEF4, "xi"), (0xCFB9, "xia"), (0xCFC6, "xian"), (0xCFE0, "xiang"),
    (0xCFF4, "xiao"), (0xD0A8, "xie"), (0xD0BD, "xin"), (0xD0C7, "xing"), (0xD0D6, "xiong"),
    (0xD0DD, "xiu"), (0xD0E6, "xu"), (0xD0F9, "xuan"), (0xD1A5, "xue"), (0xD1AB, "xun"),
    (0xD1B9, "ya"), (0xD1C9, "yan"), (0xD1EA, "yang"), (0xD1FB, "yao"), (0xD2AC, "ye"),
    (0xD2BB, "yi"), (0xD2F0, "yin"), (0xD3A2, "ying"), (0xD3B4, "yo"), (0xD3B5, "yong"),
    (0xD3C4, "you"), (0xD3D9, "yu"), (0xD4A7, "yuan"), (0xD4BB, "yue"), (0xD4C5, "yun"),
    (0xD4D1, "za"), (0xD4D4, "zai"), (0xD4DB, "zan"), (0xD4DF, "zang"), (0xD4E2, "zao"),
    (0xD4F0, "ze"), (0xD4F4, "zei"), (0xD4F5, "zen"), (0xD4F6, "zeng"), (0xD4FA, "zha"),
    (0xD5AA, "zhai"), (0xD5B0, "zhan"), (0xD5C1, "zhang"), (0xD5D0, "zhao"), (0xD5DA, "zhe"),
    (0xD5E4, "zhen"), (0xD5F4, "zheng"), (0xD6A5, "zhi"), (0xD6D0, "zhong"), (0xD6DB, "zhou"),
    (0xD6E9, "zhu"), (0xD7A5, "zhua"), (0xD7A7, "zhuai"), (0xD7A8, "zhuan"), (0xD7AE, "zhuang"),
    (0xD7B5, "zhui"), (0xD7BB, "zhun"), (0xD7BD, "zhuo"), (0xD7C8, "zi"), (0xD7D7, "zong"),
    (0xD7DE, "zou"), (0xD7E2, "zu"), (0xD7EA, "zuan"), (0xD7EC, "zui"), (0xD7F0, "zun"),
    (0xD7F2, "zuo"),
)

# 常见姓氏多音字与二级人名常用字补充
_SURNAME_OVERRIDES: dict[str, str] = {
    "单": "shan", "解": "xie", "仇": "qiu", "查": "zha", "曾": "zeng",
    "区": "ou", "繁": "po", "翟": "zhai", "覃": "qin", "朴": "piao",
    "盖": "ge", "乐": "yue", "任": "ren", "沈": "shen", "盛": "sheng",
    "尉": "yu", "万": "wan", "缪": "miao", "宁": "ning", "纪": "ji",
    "华": "hua", "俞": "yu", "柏": "bai", "贾": "jia", "殷": "yin",
    "应": "ying", "员": "yun", "折": "she", "秘": "bi", "冼": "xian",
    "褚": "chu", "费": "fei", "庞": "pang", "樊": "fan", "莫": "mo",
    "肖": "xiao", "陆": "lu", "叶": "ye", "钱": "qian", "赵": "zhao",
    "孙": "sun", "周": "zhou", "吴": "wu", "郑": "zheng", "冯": "feng",
    "陈": "chen", "蒋": "jiang", "韩": "han", "杨": "yang", "朱": "zhu",
    "秦": "qin", "许": "xu", "何": "he", "吕": "lv", "施": "shi",
    "张": "zhang", "孔": "kong", "曹": "cao", "严": "yan", "金": "jin",
    "魏": "wei", "陶": "tao", "姜": "jiang", "戚": "qi", "谢": "xie",
    "邹": "zou", "喻": "yu", "苏": "su", "潘": "pan", "葛": "ge",
    "范": "fan", "彭": "peng", "鲁": "lu", "韦": "wei", "昌": "chang",
    "马": "ma", "苗": "miao", "凤": "feng", "花": "hua", "方": "fang",
    "袁": "yuan", "柳": "liu", "鲍": "bao", "史": "shi", "唐": "tang",
    "薛": "xue", "雷": "lei", "贺": "he", "倪": "ni", "汤": "tang",
    "滕": "teng", "罗": "luo", "毕": "bi", "郝": "hao", "邬": "wu",
    "安": "an", "常": "chang", "于": "yu", "时": "shi", "傅": "fu",
    "皮": "pi", "卞": "bian", "齐": "qi", "康": "kang", "伍": "wu",
    "余": "yu", "元": "yuan", "卜": "bu", "顾": "gu", "孟": "meng",
    "平": "ping", "黄": "huang", "和": "he", "穆": "mu", "萧": "xiao",
    "尹": "yin", "姚": "yao", "邵": "shao", "湛": "zhan", "汪": "wang",
    "祁": "qi", "毛": "mao", "禹": "yu", "狄": "di", "米": "mi",
    "贝": "bei", "明": "ming", "臧": "zang", "计": "ji", "伏": "fu",
    "成": "cheng", "戴": "dai", "谈": "tan", "宋": "song", "茅": "mao",
    "熊": "xiong", "舒": "shu", "屈": "qu", "项": "xiang", "祝": "zhu",
    "董": "dong", "梁": "liang", "杜": "du", "阮": "ruan", "蓝": "lan",
    "闵": "min", "席": "xi", "季": "ji", "麻": "ma", "强": "qiang",
    "路": "lu", "娄": "lou", "危": "wei", "江": "jiang", "童": "tong",
    "颜": "yan", "郭": "guo", "梅": "mei", "林": "lin", "刁": "diao",
    "钟": "zhong", "徐": "xu", "邱": "qiu", "骆": "luo", "高": "gao",
    "夏": "xia", "蔡": "cai", "田": "tian", "樊": "fan", "胡": "hu",
    "凌": "ling", "霍": "huo", "虞": "yu", "支": "zhi", "柯": "ke",
    "管": "guan", "卢": "lu", "经": "jing", "房": "fang", "裘": "qiu",
    "干": "gan", "应": "ying", "宗": "zong", "丁": "ding", "宣": "xuan",
    "贲": "ben", "邓": "deng", "郁": "yu", "杭": "hang", "洪": "hong",
    "包": "bao", "诸": "zhu", "左": "zuo", "石": "shi", "崔": "cui",
    "吉": "ji", "钮": "niu", "龚": "gong", "程": "cheng", "嵇": "ji",
    "邢": "xing", "滑": "hua", "裴": "pei", "荣": "rong", "翁": "weng",
    "荀": "xun", "羊": "yang", "惠": "hui", "甄": "zhen", "曲": "qu",
    "家": "jia", "封": "feng", "芮": "rui", "羿": "yi", "储": "chu",
    "靳": "jin", "汲": "ji", "邴": "bing", "糜": "mi", "松": "song",
    "井": "jing", "段": "duan", "富": "fu", "巫": "wu", "乌": "wu",
    "焦": "jiao", "巴": "ba", "弓": "gong", "牧": "mu", "隗": "kui",
    "山": "shan", "谷": "gu", "车": "che", "侯": "hou", "宓": "mi",
    "蓬": "peng", "全": "quan", "郗": "xi", "班": "ban", "仰": "yang",
    "秋": "qiu", "仲": "zhong", "伊": "yi", "宫": "gong", "仇": "qiu",
    "栾": "luan", "暴": "bao", "甘": "gan", "钭": "tou", "厉": "li",
    "戎": "rong", "祖": "zu", "武": "wu", "符": "fu", "刘": "liu",
    "景": "jing", "詹": "zhan", "束": "shu", "龙": "long", "幸": "xing",
    "司": "si", "韶": "shao", "郜": "gao", "黎": "li", "蓟": "ji",
    "薄": "bo", "印": "yin", "宿": "su", "白": "bai", "怀": "huai",
    "蒲": "pu", "邰": "tai", "从": "cong", "鄂": "e", "索": "suo",
    "咸": "xian", "籍": "ji", "赖": "lai", "卓": "zhuo", "蔺": "lin",
    "屠": "tu", "蒙": "meng", "池": "chi", "乔": "qiao", "阴": "yin",
    "胥": "xu", "能": "nai", "苍": "cang", "双": "shuang", "闻": "wen",
    "莘": "shen", "党": "dang", "翟": "zhai", "谭": "tan", "贡": "gong",
    "劳": "lao", "逄": "pang", "姬": "ji", "申": "shen", "扶": "fu",
    "堵": "du", "冉": "ran", "宰": "zai", "郦": "li", "雍": "yong",
    "却": "que", "璩": "qu", "桑": "sang", "桂": "gui", "濮": "pu",
    "牛": "niu", "寿": "shou", "通": "tong", "边": "bian", "扈": "hu",
    "燕": "yan", "冀": "ji", "郏": "jia", "浦": "pu", "尚": "shang",
    "农": "nong", "温": "wen", "别": "bie", "庄": "zhuang", "晏": "yan",
    "柴": "chai", "瞿": "qu", "阎": "yan", "充": "chong", "慕": "mu",
    "连": "lian", "茹": "ru", "习": "xi", "宦": "huan", "艾": "ai",
    "鱼": "yu", "容": "rong", "向": "xiang", "古": "gu", "易": "yi",
    "慎": "shen", "戈": "ge", "廖": "liao", "庾": "yu", "终": "zhong",
    "暨": "ji", "居": "ju", "衡": "heng", "步": "bu", "都": "du",
    "耿": "geng", "满": "man", "弘": "hong", "匡": "kuang", "国": "guo",
    "文": "wen", "寇": "kou", "广": "guang", "禄": "lu", "阙": "que",
    "东": "dong", "欧": "ou", "殳": "shu", "沃": "wo", "利": "li",
    "蔚": "yu", "越": "yue", "夔": "kui", "隆": "long", "师": "shi",
    "巩": "gong", "厍": "she", "聂": "nie", "晁": "chao", "勾": "gou",
    "敖": "ao", "融": "rong", "冷": "leng", "訾": "zi", "辛": "xin",
    "阚": "kan", "那": "na", "简": "jian", "饶": "rao", "空": "kong",
    "曾": "zeng", "毋": "wu", "沙": "sha", "乜": "nie", "养": "yang",
    "鞠": "ju", "须": "xu", "丰": "feng", "巢": "chao", "关": "guan",
    "蒯": "kuai", "相": "xiang", "查": "zha", "后": "hou", "荆": "jing",
    "红": "hong", "游": "you", "竺": "zhu", "权": "quan", "逯": "lu",
    "盖": "ge", "益": "yi", "桓": "huan", "公": "gong", "王": "wang",
    "李": "li",
}

_GIVEN_NAME_EXTRAS: dict[str, str] = {
    "鑫": "xin", "楠": "nan", "皓": "hao", "睿": "rui", "琪": "qi",
    "婷": "ting", "梓": "zi", "怡": "yi", "婧": "jing", "媛": "yuan",
    "淼": "miao", "轩": "xuan", "涵": "han", "璧": "bi", "壁": "bi",
    "岚": "lan", "煜": "yu", "昊": "hao", "晟": "sheng", "铭": "ming",
    "钰": "yu", "锐": "rui", "铮": "zheng", "霖": "lin", "雯": "wen",
    "霏": "fei", "馨": "xin", "玥": "yue", "琳": "lin", "琦": "qi",
    "瑶": "yao", "璐": "lu", "璇": "xuan", "彤": "tong", "彦": "yan",
    "彬": "bin", "炫": "xuan", "烨": "ye", "炜": "wei", "焕": "huan",
    "茜": "qian", "茗": "ming", "菲": "fei", "萱": "xuan", "薇": "wei",
    "宸": "chen", "宇": "yu", "泽": "ze", "浩": "hao", "涛": "tao",
    "瀚": "han", "泓": "hong", "潇": "xiao", "沐": "mu", "沛": "pei",
    "峻": "jun", "峥": "zheng", "嵘": "rong", "昱": "yu", "昕": "xin",
    "晨": "chen", "曦": "xi", "晗": "han", "昭": "zhao", "晖": "hui",
    "旭": "xu", "畅": "chang", "奕": "yi", "弈": "yi", "翊": "yi",
    "翔": "xiang", "翰": "han", "翱": "ao", "喆": "zhe", "哲": "zhe",
    "嘉": "jia", "硕": "shuo", "磊": "lei", "磊": "lei", "坤": "kun",
    "培": "pei", "均": "jun", "城": "cheng", "峰": "feng", "凯": "kai",
    "博": "bo", "毅": "yi", "恒": "heng", "悦": "yue", "慧": "hui",
    "思": "si", "念": "nian", "悠": "you", "然": "ran", "熙": "xi",
    "阳": "yang", "明": "ming", "亮": "liang", "杰": "jie", "俊": "jun",
    "伟": "wei", "勇": "yong", "军": "jun", "刚": "gang", "强": "qiang",
    "敏": "min", "静": "jing", "丽": "li", "娟": "juan", "燕": "yan",
    "艳": "yan", "玲": "ling", "霞": "xia", "芳": "fang", "秀": "xiu",
    "华": "hua", "平": "ping", "波": "bo", "鹏": "peng", "朋": "peng",
    "超": "chao", "飞": "fei", "龙": "long", "辉": "hui", "斌": "bin",
    "兵": "bing", "涛": "tao", "健": "jian", "宁": "ning", "乐": "le",
    "萌": "meng", "蕾": "lei", "萍": "ping", "颖": "ying", "雪": "xue",
    "梅": "mei", "兰": "lan", "竹": "zhu", "菊": "ju", "欣": "xin",
    "佳": "jia", "美": "mei", "晓": "xiao", "小": "xiao", "大": "da",
    "志": "zhi", "建": "jian", "国": "guo", "文": "wen", "海": "hai",
    "东": "dong", "南": "nan", "西": "xi", "北": "bei", "中": "zhong",
    "正": "zheng", "清": "qing", "新": "xin", "荣": "rong", "春": "chun",
    "夏": "xia", "秋": "qiu", "冬": "dong", "云": "yun", "风": "feng",
    "雨": "yu", "霜": "shuang", "冰": "bing", "洁": "jie", "玉": "yu",
    "珍": "zhen", "珠": "zhu", "瑞": "rui", "祥": "xiang", "福": "fu",
    "安": "an", "康": "kang", "顺": "shun", "兴": "xing", "发": "fa",
    "财": "cai", "富": "fu", "贵": "gui", "雅": "ya", "韵": "yun",
    "诗": "shi", "书": "shu", "画": "hua", "琴": "qin", "棋": "qi",
    "剑": "jian", "锋": "feng", "威": "wei", "武": "wu", "雄": "xiong",
    "英": "ying", "豪": "hao", "杰": "jie", "才": "cai", "德": "de",
    "仁": "ren", "义": "yi", "礼": "li", "智": "zhi", "信": "xin",
    "忠": "zhong", "孝": "xiao", "诚": "cheng", "谦": "qian", "恭": "gong",
    "敬": "jing", "勤": "qin", "勉": "mian", "励": "li", "进": "jin",
    "达": "da", "通": "tong", "成": "cheng", "功": "gong", "业": "ye",
    "立": "li", "宏": "hong", "伟": "wei", "博": "bo", "远": "yuan",
    "广": "guang", "深": "shen", "厚": "hou", "重": "zhong", "高": "gao",
    "山": "shan", "川": "chuan", "河": "he", "湖": "hu", "江": "jiang",
    "森": "sen", "林": "lin", "木": "mu", "松": "song", "柏": "bo",
    "杨": "yang", "柳": "liu", "枫": "feng", "桦": "hua", "桐": "tong",
    "榕": "rong", "樱": "ying", "桃": "tao", "杏": "xing", "桂": "gui",
    "荷": "he", "莲": "lian", "蓉": "rong", "芝": "zhi", "芷": "zhi",
    "若": "ruo", "芸": "yun", "苏": "su", "苏": "su", "茉": "mo",
    "莉": "li", "莎": "sha", "萌": "meng", "蔚": "wei", "蔓": "man",
    "蔡": "cai", "加": "jia", "涌": "yong", "会": "hui", "岩": "yan",
    "园": "yuan", "力": "li", "丁": "ding", "特": "te", "周": "zhou",
}


def _char_to_pinyin(ch: str, is_first: bool = False) -> str:
    if ch.isascii():
        return ch.lower() if ch.isalnum() else ""
    if is_first and ch in _SURNAME_OVERRIDES:
        return _SURNAME_OVERRIDES[ch]
    if ch in _GIVEN_NAME_EXTRAS:
        return _GIVEN_NAME_EXTRAS[ch]
    try:
        gb = ch.encode("gb2312")
    except UnicodeEncodeError:
        return ""
    if len(gb) != 2:
        return ""
    code = (gb[0] << 8) + gb[1]
    if code < 0xB0A1 or code > 0xD7F9:
        return ""
    # 二分查找 GB2312 一级字表边界
    lo, hi = 0, len(_GB2312_PINYIN_BOUNDARIES) - 1
    ans = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        if _GB2312_PINYIN_BOUNDARIES[mid][0] <= code:
            ans = _GB2312_PINYIN_BOUNDARIES[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def name_to_pinyin(name: str) -> str:
    """将中文姓名转为小写全拼（例如 '王秀丽' -> 'wangxiuli'）。"""
    cleaned = re.sub(r"\s+", "", (name or "").strip())
    if not cleaned:
        return ""
    parts = [_char_to_pinyin(ch, is_first=(idx == 0)) for idx, ch in enumerate(cleaned)]
    out = "".join(p for p in parts if p)
    return re.sub(r"[^a-z0-9]", "", out.lower())


def split_owners(raw: str | None) -> list[str]:
    """拆分项目负责人字段（支持 '李力丁、康特'、'张三,李四' 等多负责人格式）。"""
    if not raw:
        return []
    tokens = re.split(r"[、,，;/；|]+", str(raw).strip())
    return [t.strip() for t in tokens if t.strip()]


def owner_matches_user(user: dict | None, owner_raw: str | None) -> bool:
    """严格判断某项目的 owner 字段是否显式指向该用户（用于邮件投递，不因 admin 角色匹配全量项目）。"""
    if not user:
        return False
    owners = split_owners(owner_raw)
    if not owners:
        return False
    display = str(user.get("display_name") or "").strip()
    username = str(user.get("username") or "").strip().lower()
    for owner in owners:
        if display and owner == display:
            return True
        if username and owner.lower() == username:
            return True
        if username and name_to_pinyin(owner) == username:
            return True
    return False


def user_owns_project(user: dict | None, project: dict) -> bool:
    """判断用户是否有权访问该项目（superadmin/admin 全部可见；普通用户按负责人匹配）。"""
    if not user:
        return False
    role = user.get("role")
    if role in ("superadmin", "admin"):
        return True
    return owner_matches_user(user, project.get("owner"))


def hash_password(password: str, salt: str | None = None) -> str:
    s = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), s.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256$120000${s}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iter_str, salt, expected_hex = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iters = int(iter_str)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iters)
    return hmac.compare_digest(dk.hex(), expected_hex)


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class PlatformDB:
    """线程安全的轻量 SQLite 管理端存储。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    email TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    email_notify TEXT NOT NULL DEFAULT 'batch',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_username TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    client_ip TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(id DESC);
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    project_name TEXT NOT NULL DEFAULT '',
                    department TEXT NOT NULL DEFAULT '',
                    owner TEXT NOT NULL DEFAULT '',
                    git_url TEXT NOT NULL DEFAULT '',
                    branch TEXT NOT NULL DEFAULT 'main',
                    status TEXT NOT NULL DEFAULT '在研',
                    tech_stack TEXT NOT NULL DEFAULT '',
                    project_type TEXT NOT NULL DEFAULT '',
                    core_flows TEXT NOT NULL DEFAULT '',
                    scope_out TEXT NOT NULL DEFAULT '',
                    materials_dir TEXT NOT NULL DEFAULT '',
                    weights TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    loc_total INTEGER,
                    file_count INTEGER,
                    languages_json TEXT NOT NULL DEFAULT '{}',
                    last_commit_at TEXT,
                    last_commit_author TEXT NOT NULL DEFAULT '',
                    last_commit_subject TEXT NOT NULL DEFAULT '',
                    metrics_updated_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prompt_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    updated_by TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_prompt_versions_key ON prompt_versions(prompt_key, version DESC);
                CREATE TABLE IF NOT EXISTS git_credentials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label TEXT NOT NULL DEFAULT '',
                    protocol TEXT NOT NULL DEFAULT 'https',
                    host TEXT NOT NULL,
                    path_prefix TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS email_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    recipient_email TEXT NOT NULL,
                    recipient_name TEXT NOT NULL DEFAULT '',
                    recipient_type TEXT NOT NULL DEFAULT 'owner',
                    project_ids_json TEXT NOT NULL DEFAULT '[]',
                    subject TEXT NOT NULL DEFAULT '',
                    attachment_names_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'sent',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_email_logs_created ON email_logs(id DESC);
                CREATE TABLE IF NOT EXISTS issue_triages (
                    project_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    item_id TEXT NOT NULL DEFAULT '',
                    level TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    evidence_text TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    reason TEXT NOT NULL DEFAULT '',
                    updated_by TEXT NOT NULL DEFAULT '',
                    updated_role TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_issue_triages_project ON issue_triages(project_id, status);
                """
            )
            # 兼容已有数据库：为 projects 表补齐 VCS 类型与 Git/SVN 连通性巡检字段
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
            for col_name, col_def in (
                ("vcs_type", "TEXT NOT NULL DEFAULT 'git'"),
                ("git_check_ok", "INTEGER"),
                ("git_check_error", "TEXT NOT NULL DEFAULT ''"),
                ("git_checked_at", "TEXT"),
            ):
                if col_name not in cols:
                    conn.execute(f"ALTER TABLE projects ADD COLUMN {col_name} {col_def}")

    @staticmethod
    def _public_user(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
            "email": row["email"] or "",
            "must_change_password": bool(row["must_change_password"]),
            "email_notify": row["email_notify"] or "batch",
            "is_active": bool(row["is_active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_login_at": row["last_login_at"],
        }

    def ensure_initial_superadmin(self, legacy_token: str = "") -> tuple[str, str] | None:
        """若尚无超级管理员，自动初始化一个（返回 (username, initial_password) 仅在新建时非空）。"""
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM users WHERE role = 'superadmin' LIMIT 1"
            ).fetchone()
            if existing:
                return None
            username = (os.environ.get("AUDIT_INIT_ADMIN_USER") or "admin").strip()
            if not USERNAME_RE.match(username):
                username = "admin"
            env_pass = (os.environ.get("AUDIT_INIT_ADMIN_PASS") or "").strip()
            if env_pass:
                password = env_pass
                must_change = 0
            elif legacy_token and len(legacy_token.strip()) >= 6:
                password = legacy_token.strip()
                must_change = 1
            else:
                password = secrets.token_urlsafe(12)
                must_change = 1
            now = _now_iso()
            conn.execute(
                """
                INSERT INTO users (username, display_name, role, email, password_hash,
                                   must_change_password, email_notify, is_active, created_at, updated_at)
                VALUES (?, ?, 'superadmin', ?, ?, ?, 'batch', 1, ?, ?)
                """,
                (username, "系统运维管理员", f"{username}@shineyue.com",
                 hash_password(password), must_change, now, now),
            )
            return username, password

    # ---------- 认证与会话 ----------

    def authenticate(self, username: str, password: str) -> dict | None:
        uname = (username or "").strip()
        if not uname or not password:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE lower(username) = lower(?) LIMIT 1",
                (uname,),
            ).fetchone()
            if not row or not row["is_active"]:
                return None
            if not verify_password(password, row["password_hash"]):
                return None
            now = _now_iso()
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, row["id"]))
            updated = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
            return self._public_user(updated)

    def create_session(self, user_id: int, ttl_sec: int = SESSION_TTL_SEC) -> str:
        token = secrets.token_urlsafe(32)
        now = dt.datetime.now()
        exp = now.timestamp() + ttl_sec
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.timestamp(),))
            conn.execute(
                "INSERT INTO sessions (token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (token, user_id, exp, now.isoformat(timespec="seconds")),
            )
        return token

    def get_session_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        now_ts = dt.datetime.now().timestamp()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT u.* FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = ? AND s.expires_at > ? AND u.is_active = 1
                LIMIT 1
                """,
                (token, now_ts),
            ).fetchone()
            return self._public_user(row)

    def revoke_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def revoke_user_sessions(self, user_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    # ---------- 用户 CRUD ----------

    def list_users(self) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY CASE role WHEN 'superadmin' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, id ASC"
            ).fetchall()
            return [self._public_user(r) for r in rows if r is not None]

    def get_user(self, user_id: int) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return self._public_user(row)

    def create_user(self, *, username: str, display_name: str, role: str,
                    password: str, email: str = "", must_change_password: bool = True,
                    email_notify: str = "batch") -> dict:
        uname = (username or "").strip().lower()
        dname = (display_name or "").strip() or uname
        em = (email or "").strip()
        if not USERNAME_RE.match(uname):
            raise ValueError("用户名需为 2-64 位字母、数字、点、横线或下划线")
        if role not in ROLES:
            raise ValueError(f"角色必须是 {', '.join(ROLES)}")
        if len(password or "") < 6:
            raise ValueError("密码长度不能少于 6 位")
        if em and not EMAIL_RE.match(em):
            raise ValueError(f"邮箱格式无效：{em}")
        if email_notify not in EMAIL_NOTIFY_MODES:
            email_notify = "batch"
        now = _now_iso()
        with self._lock, self._connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO users (username, display_name, role, email, password_hash,
                                       must_change_password, email_notify, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (uname, dname, role, em, hash_password(password),
                     1 if must_change_password else 0, email_notify, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"用户名已存在：{uname}") from exc
            row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
            assert row is not None
            return self._public_user(row)  # type: ignore[return-value]

    def update_user(self, user_id: int, *, display_name: str | None = None,
                    role: str | None = None, email: str | None = None,
                    is_active: bool | None = None, email_notify: str | None = None) -> dict:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise KeyError("用户不存在")
            next_role = role if role is not None else row["role"]
            next_active = int(bool(is_active)) if is_active is not None else int(row["is_active"])
            if next_role not in ROLES:
                raise ValueError(f"角色必须是 {', '.join(ROLES)}")
            # 防止停用或降级最后一个启用的超级管理员
            if row["role"] == "superadmin" and (next_role != "superadmin" or not next_active):
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role = 'superadmin' AND is_active = 1 AND id != ?",
                    (user_id,),
                ).fetchone()[0]
                if cnt < 1:
                    raise ValueError("系统至少须保留一个启用的超级管理员")
            next_display = (display_name.strip() if display_name is not None else row["display_name"]) or row["username"]
            next_email = email.strip() if email is not None else (row["email"] or "")
            if next_email and not EMAIL_RE.match(next_email):
                raise ValueError(f"邮箱格式无效：{next_email}")
            next_notify = email_notify if email_notify in EMAIL_NOTIFY_MODES else (row["email_notify"] or "batch")
            conn.execute(
                """
                UPDATE users
                SET display_name = ?, role = ?, email = ?, is_active = ?, email_notify = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_display, next_role, next_email, next_active, next_notify, _now_iso(), user_id),
            )
            if not next_active:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            updated = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            assert updated is not None
            return self._public_user(updated)  # type: ignore[return-value]

    def change_password(self, user_id: int, new_password: str, *,
                        old_password: str | None = None, must_change_password: bool = False) -> None:
        if len(new_password or "") < 6:
            raise ValueError("新密码长度不能少于 6 位")
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise KeyError("用户不存在")
            if old_password is not None and not verify_password(old_password, row["password_hash"]):
                raise ValueError("原密码不正确")
            conn.execute(
                "UPDATE users SET password_hash = ?, must_change_password = ?, updated_at = ? WHERE id = ?",
                (hash_password(new_password), 1 if must_change_password else 0, _now_iso(), user_id),
            )

    def delete_user(self, user_id: int, actor_id: int | None = None) -> None:
        if actor_id is not None and user_id == actor_id:
            raise ValueError("不能删除当前正在登录的账号")
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise KeyError("用户不存在")
            if row["role"] == "superadmin":
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role = 'superadmin' AND is_active = 1 AND id != ?",
                    (user_id,),
                ).fetchone()[0]
                if cnt < 1:
                    raise ValueError("不能删除系统最后一个超级管理员")
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def preview_users_from_owners(self, owner_names: list[str], email_domain: str = "shineyue.com") -> list[dict]:
        """从项目负责人名单生成建议的用户名与 <姓名全拼>@shineyue.com 邮箱预览列表。"""
        domain = (email_domain or "shineyue.com").strip().lstrip("@") or "shineyue.com"
        existing = self.list_users()
        existing_by_name = {u["display_name"]: u for u in existing}
        existing_usernames = {u["username"].lower() for u in existing}
        used_usernames = set(existing_usernames)

        unique_names: list[str] = []
        seen_names: set[str] = set()
        for raw in owner_names:
            for name in split_owners(raw):
                if name not in seen_names:
                    seen_names.add(name)
                    unique_names.append(name)

        preview: list[dict] = []
        for display_name in sorted(unique_names):
            ex = existing_by_name.get(display_name)
            if ex:
                preview.append({
                    "display_name": display_name,
                    "username": ex["username"],
                    "email": ex["email"],
                    "role": ex["role"],
                    "exists": True,
                })
                continue
            base = name_to_pinyin(display_name)
            if not base or len(base) < 2:
                base = f"user_{hashlib.md5(display_name.encode('utf-8')).hexdigest()[:6]}"
            cand = base
            suffix = 2
            while cand in used_usernames:
                cand = f"{base}{suffix}"
                suffix += 1
            used_usernames.add(cand)
            preview.append({
                "display_name": display_name,
                "username": cand,
                "email": f"{cand}@{domain}",
                "role": "user",
                "exists": False,
            })
        return preview

    # ---------- 审计日志 ----------

    def record_audit(self, actor: dict | None, action: str, target: str = "",
                     detail: str | dict = "", client_ip: str = "") -> None:
        uname = (actor or {}).get("username", "system")
        role = (actor or {}).get("role", "system")
        detail_str = json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else str(detail or "")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs (actor_username, actor_role, action, target, detail, client_ip, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (uname, role, action, target[:200], detail_str[:1000], client_ip[:64], _now_iso()),
            )

    def list_audit_logs(self, limit: int = 150) -> list[dict]:
        lim = max(1, min(500, int(limit or 150)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (lim,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- 平台配置键值 ----------

    def get_setting(self, key: str, default: object = None) -> object:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM platform_settings WHERE key = ?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except ValueError:
                return row["value"]

    def set_setting(self, key: str, value: object) -> None:
        raw = json.dumps(value, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, raw, _now_iso()),
            )

    def get_stale_thresholds(self) -> dict[str, int]:
        raw = self.get_setting("stale_thresholds")
        warn_days, crit_days = 90, 180
        if isinstance(raw, dict):
            try:
                w = int(raw.get("warn_days", 90))
                c = int(raw.get("critical_days", 180))
                if 7 <= w < c <= 3650:
                    warn_days, crit_days = w, c
            except (TypeError, ValueError):
                pass
        return {"warn_days": warn_days, "critical_days": crit_days}

    def set_stale_thresholds(self, warn_days: int, critical_days: int) -> dict[str, int]:
        try:
            w = int(warn_days)
            c = int(critical_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("长期未维护阈值必须为整数天数") from exc
        if w < 7 or c <= w or c > 3650:
            raise ValueError("预警天数须满足 7 <= 黄色预警天数 < 红色预警天数 <= 3650")
        data = {"warn_days": w, "critical_days": c}
        self.set_setting("stale_thresholds", data)
        return data

    # ---------- 项目管理与 CSV 双向同步 ----------

    @staticmethod
    def _row_to_project(row: sqlite3.Row) -> dict:
        try:
            langs = json.loads(row["languages_json"] or "{}")
            if not isinstance(langs, dict):
                langs = {}
        except ValueError:
            langs = {}
        keys = set(row.keys())
        out = {field: (row[field] or "") if field in keys else "" for field in PROJECT_CSV_FIELDS}
        out["vcs_type"] = vcs.detect_vcs_type(out.get("git_url", ""), out.get("vcs_type") or None)
        out.update({
            "loc_total": row["loc_total"],
            "file_count": row["file_count"],
            "languages": langs,
            "last_commit_at": row["last_commit_at"],
            "last_commit_author": row["last_commit_author"] or "",
            "last_commit_subject": row["last_commit_subject"] or "",
            "metrics_updated_at": row["metrics_updated_at"],
            "git_check_ok": None if row["git_check_ok"] is None else bool(row["git_check_ok"]),
            "git_check_error": row["git_check_error"] or "",
            "git_checked_at": row["git_checked_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
        return out

    @staticmethod
    def validate_project_payload(data: dict) -> dict[str, str]:
        pid = str(data.get("project_id") or "").strip()
        if not PROJECT_ID_RE.match(pid):
            raise ValueError("project_id 仅允许字母/数字开头，包含字母、数字、点、横线或下划线（1-128 位）")
        raw_url = str(data.get("git_url") or data.get("repo_url") or "").strip()
        explicit_vcs = str(data.get("vcs_type") or "").strip().lower()
        if explicit_vcs and explicit_vcs not in vcs.VALID_VCS_TYPES:
            raise ValueError("仓库类型仅支持 git 或 svn")
        vcs_type = vcs.detect_vcs_type(raw_url, explicit_vcs or None)
        git_url = validate_git_url(raw_url, vcs_type=vcs_type)
        raw_branch = str(data.get("branch") or "").strip()
        if vcs_type == "svn":
            branch = raw_branch
            if branch:
                vcs.compose_svn_url(git_url, branch)
        else:
            branch = raw_branch or "main"
            if branch.startswith("-") or any(ch.isspace() for ch in branch) or ".." in branch:
                raise ValueError(f"分支名格式无效：{branch}")
        status = str(data.get("status") or "在研").strip() or "在研"
        if status not in VALID_PROJECT_STATUSES:
            raise ValueError(f"状态必须是 {' / '.join(VALID_PROJECT_STATUSES)}")
        cleaned = {field: str(data.get(field) or "").strip() for field in PROJECT_CSV_FIELDS}
        cleaned["project_id"] = pid
        cleaned["project_name"] = cleaned["project_name"] or pid
        cleaned["vcs_type"] = vcs_type
        cleaned["git_url"] = git_url
        cleaned["branch"] = branch
        cleaned["status"] = status
        return cleaned

    def export_projects_csv(self, target_csv: Path) -> None:
        rows = self.list_projects()
        target_csv.parent.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(PROJECT_CSV_FIELDS), extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in PROJECT_CSV_FIELDS})
        tmp = target_csv.with_suffix(".csv.tmp")
        tmp.write_text(buf.getvalue(), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, target_csv)

    def seed_projects_from_csv(self, source_csv: Path, managed_csv: Path) -> int:
        """首次升级时把只读挂载的 projects.csv 迁移进 SQLite，并生成可写 managed CSV。"""
        with self._lock, self._connect() as conn:
            existing_cnt = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        if existing_cnt > 0:
            if not managed_csv.is_file():
                self.export_projects_csv(managed_csv)
            return 0
        if not source_csv.is_file():
            return 0
        imported = 0
        now = _now_iso()
        with source_csv.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            with self._lock, self._connect() as conn:
                for row in reader:
                    pid = str(row.get("project_id") or "").strip()
                    if not pid or pid.startswith("#"):
                        continue
                    git_url = str(row.get("git_url") or row.get("repo_url") or "").strip()
                    vcs_type = vcs.detect_vcs_type(git_url, str(row.get("vcs_type") or "").strip() or None)
                    raw_branch = str(row.get("branch") or "").strip()
                    branch = raw_branch if vcs_type == "svn" else (raw_branch or "main")
                    vals = [
                        pid,
                        str(row.get("project_name") or pid).strip(),
                        str(row.get("department") or "").strip(),
                        str(row.get("owner") or "").strip(),
                        vcs_type,
                        git_url,
                        branch,
                        str(row.get("status") or "在研").strip() or "在研",
                        str(row.get("tech_stack") or "").strip(),
                        str(row.get("project_type") or "").strip(),
                        str(row.get("core_flows") or "").strip(),
                        str(row.get("scope_out") or "").strip(),
                        str(row.get("materials_dir") or "").strip(),
                        str(row.get("weights") or "").strip(),
                        str(row.get("notes") or "").strip(),
                        now,
                        now,
                    ]
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO projects (
                            project_id, project_name, department, owner, vcs_type, git_url, branch, status,
                            tech_stack, project_type, core_flows, scope_out, materials_dir, weights, notes,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        vals,
                    )
                    imported += 1
        if imported > 0:
            self.export_projects_csv(managed_csv)
        return imported

    def list_projects(self) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY CASE status WHEN '在研' THEN 0 WHEN '停用' THEN 1 ELSE 2 END, department ASC, project_id ASC"
            ).fetchall()
            return [self._row_to_project(r) for r in rows]

    def get_project(self, project_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
            return self._row_to_project(row) if row else None

    def upsert_project(self, data: dict, *, create_only: bool = False,
                       managed_csv: Path | None = None) -> dict:
        cleaned = self.validate_project_payload(data)
        now = _now_iso()
        with self._lock, self._connect() as conn:
            exists = conn.execute(
                "SELECT project_id, vcs_type, git_url, branch FROM projects WHERE project_id = ?", (cleaned["project_id"],)
            ).fetchone()
            if create_only and exists:
                raise ValueError(f"项目 ID 已存在：{cleaned['project_id']}")
            if exists:
                git_changed = (
                    (exists["vcs_type"] or "git") != cleaned["vcs_type"]
                    or exists["git_url"] != cleaned["git_url"]
                    or exists["branch"] != cleaned["branch"]
                )
                conn.execute(
                    """
                    UPDATE projects
                    SET project_name=?, department=?, owner=?, vcs_type=?, git_url=?, branch=?, status=?,
                        tech_stack=?, project_type=?, core_flows=?, scope_out=?, materials_dir=?,
                        weights=?, notes=?, updated_at=?,
                        git_check_ok=CASE WHEN ? THEN NULL ELSE git_check_ok END,
                        git_check_error=CASE WHEN ? THEN '' ELSE git_check_error END
                    WHERE project_id=?
                    """,
                    (
                        cleaned["project_name"], cleaned["department"], cleaned["owner"],
                        cleaned["vcs_type"], cleaned["git_url"], cleaned["branch"], cleaned["status"],
                        cleaned["tech_stack"], cleaned["project_type"], cleaned["core_flows"],
                        cleaned["scope_out"], cleaned["materials_dir"], cleaned["weights"],
                        cleaned["notes"], now,
                        1 if git_changed else 0, 1 if git_changed else 0,
                        cleaned["project_id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO projects (
                        project_id, project_name, department, owner, vcs_type, git_url, branch, status,
                        tech_stack, project_type, core_flows, scope_out, materials_dir, weights, notes,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cleaned["project_id"], cleaned["project_name"], cleaned["department"],
                        cleaned["owner"], cleaned["vcs_type"], cleaned["git_url"], cleaned["branch"], cleaned["status"],
                        cleaned["tech_stack"], cleaned["project_type"], cleaned["core_flows"],
                        cleaned["scope_out"], cleaned["materials_dir"], cleaned["weights"],
                        cleaned["notes"], now, now,
                    ),
                )
            row = conn.execute("SELECT * FROM projects WHERE project_id = ?", (cleaned["project_id"],)).fetchone()
            assert row is not None
            proj = self._row_to_project(row)
        if managed_csv is not None:
            self.export_projects_csv(managed_csv)
        return proj

    def batch_update_project_status(self, project_ids: list[str], status: str, *,
                                    managed_csv: Path | None = None) -> dict:
        target_status = (status or "").strip()
        if target_status not in VALID_PROJECT_STATUSES:
            raise ValueError(f"状态必须是 {' / '.join(VALID_PROJECT_STATUSES)}")
        clean_ids = [str(x).strip() for x in project_ids if str(x).strip()]
        clean_ids = list(dict.fromkeys(clean_ids))
        if not clean_ids:
            raise ValueError("请至少选择一个项目")
        now = _now_iso()
        placeholders = ",".join("?" for _ in clean_ids)
        with self._lock, self._connect() as conn:
            existing = [
                r["project_id"]
                for r in conn.execute(
                    f"SELECT project_id FROM projects WHERE project_id IN ({placeholders})",
                    clean_ids,
                ).fetchall()
            ]
            if existing:
                ph2 = ",".join("?" for _ in existing)
                conn.execute(
                    f"UPDATE projects SET status = ?, updated_at = ? WHERE project_id IN ({ph2})",
                    [target_status, now, *existing],
                )
        if managed_csv is not None and existing:
            self.export_projects_csv(managed_csv)
        return {"updated": len(existing), "status": target_status, "project_ids": existing}

    def batch_update_projects_meta(
        self,
        project_ids: list[str],
        *,
        department: str | None = None,
        owner: str | None = None,
        owner_mode: str = "replace",
        managed_csv: Path | None = None,
    ) -> dict:
        """原子批量调整项目所属部门和/或负责人（支持替换、追加、移除）。"""
        if owner_mode not in ("replace", "append", "remove"):
            raise ValueError("owner_mode 必须是 replace、append 或 remove")
        clean_ids = list(dict.fromkeys(str(x).strip() for x in project_ids if str(x).strip()))
        if not clean_ids:
            raise ValueError("请至少选择一个项目")
        set_dept = department is not None
        set_owner = owner is not None
        if not set_dept and not set_owner:
            raise ValueError("请至少指定要修改的部门或负责人")
        dept_val = (department or "").strip()[:100]
        owner_tokens = [s.strip() for s in re.split(r"[、,，/;；]+", (owner or "").strip()) if s.strip()]
        if set_owner and owner_mode in ("append", "remove") and not owner_tokens:
            raise ValueError("追加或移除模式必须填写负责人姓名")

        now = _now_iso()
        placeholders = ",".join("?" for _ in clean_ids)
        updated_ids: list[str] = []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT project_id, department, owner FROM projects WHERE project_id IN ({placeholders})",
                clean_ids,
            ).fetchall()
            for r in rows:
                pid = r["project_id"]
                new_dept = dept_val if set_dept else (r["department"] or "")
                new_owner = r["owner"] or ""
                if set_owner:
                    if owner_mode == "replace":
                        new_owner = "、".join(dict.fromkeys(owner_tokens))
                    else:
                        cur_list = [s.strip() for s in re.split(r"[、,，/;；]+", new_owner) if s.strip()]
                        if owner_mode == "append":
                            merged = list(dict.fromkeys([*cur_list, *owner_tokens]))
                            new_owner = "、".join(merged)
                        elif owner_mode == "remove":
                            rem_set = set(owner_tokens)
                            new_owner = "、".join([x for x in cur_list if x not in rem_set])
                conn.execute(
                    "UPDATE projects SET department = ?, owner = ?, updated_at = ? WHERE project_id = ?",
                    (new_dept, new_owner, now, pid),
                )
                updated_ids.append(pid)
        if managed_csv is not None and updated_ids:
            self.export_projects_csv(managed_csv)
        return {"updated": len(updated_ids), "project_ids": updated_ids}

    def list_project_triages(self, project_id: str) -> dict[str, dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM issue_triages WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            return {
                r["fingerprint"]: {
                    "project_id": r["project_id"],
                    "fingerprint": r["fingerprint"],
                    "item_id": r["item_id"],
                    "level": r["level"],
                    "title": r["title"],
                    "evidence_text": r["evidence_text"],
                    "status": r["status"],
                    "reason": r["reason"],
                    "updated_by": r["updated_by"],
                    "updated_role": r["updated_role"],
                    "updated_at": r["updated_at"],
                }
                for r in rows
            }

    def count_exempted_by_project(self) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT project_id, COUNT(*) AS cnt FROM issue_triages WHERE status = 'exempted' GROUP BY project_id"
            ).fetchall()
            return {r["project_id"]: int(r["cnt"]) for r in rows}

    def upsert_issue_triage(
        self,
        *,
        project_id: str,
        fingerprint: str,
        status: str,
        reason: str = "",
        item_id: str = "",
        level: str = "",
        title: str = "",
        evidence_text: str = "",
        actor: dict | None = None,
    ) -> dict:
        st = (status or "open").strip().lower()
        if st not in ISSUE_TRIAGE_STATUSES:
            raise ValueError(f"处置状态仅支持 {' / '.join(ISSUE_TRIAGE_STATUSES)}")
        fp = (fingerprint or "").strip()
        if not fp or len(fp) > 64:
            raise ValueError("无效的问题指纹")
        rs = (reason or "").strip()[:500]
        if st == "exempted" and not rs:
            raise ValueError("标记为「误报 / 风险豁免」时必须填写豁免原因")
        now = _now_iso()
        actor_name = (actor or {}).get("display_name") or (actor or {}).get("username") or "system"
        actor_role = (actor or {}).get("role") or ""
        with self._lock, self._connect() as conn:
            if st == "open" and not rs:
                conn.execute(
                    "DELETE FROM issue_triages WHERE project_id = ? AND fingerprint = ?",
                    (project_id, fp),
                )
                return {
                    "project_id": project_id,
                    "fingerprint": fp,
                    "item_id": item_id,
                    "level": level,
                    "title": title,
                    "evidence_text": evidence_text,
                    "status": "open",
                    "reason": "",
                    "updated_by": actor_name,
                    "updated_role": actor_role,
                    "updated_at": now,
                }
            conn.execute(
                """
                INSERT INTO issue_triages (
                    project_id, fingerprint, item_id, level, title, evidence_text,
                    status, reason, updated_by, updated_role, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, fingerprint) DO UPDATE SET
                    item_id = excluded.item_id,
                    level = excluded.level,
                    title = excluded.title,
                    evidence_text = excluded.evidence_text,
                    status = excluded.status,
                    reason = excluded.reason,
                    updated_by = excluded.updated_by,
                    updated_role = excluded.updated_role,
                    updated_at = excluded.updated_at
                """,
                (
                    project_id, fp, item_id[:40], level[:16], title[:240], evidence_text[:300],
                    st, rs, actor_name[:80], actor_role[:32], now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM issue_triages WHERE project_id = ? AND fingerprint = ?",
                (project_id, fp),
            ).fetchone()
            assert row is not None
            return dict(row)

    def get_retention_policy(self) -> dict[str, int]:
        raw = self.get_setting("storage_retention_policy")
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    days = max(7, min(3650, int(data.get("keep_days", 90))))
                    runs = max(5, min(500, int(data.get("keep_latest", 30))))
                    return {"keep_days": days, "keep_latest": runs}
            except (ValueError, TypeError):
                pass
        return {"keep_days": 90, "keep_latest": 30}

    def set_retention_policy(self, keep_days: int, keep_latest: int) -> dict[str, int]:
        days = int(keep_days)
        runs = int(keep_latest)
        if days < 7 or days > 3650:
            raise ValueError("保留天数必须在 7 到 3650 之间")
        if runs < 5 or runs > 500:
            raise ValueError("最少保留批次数必须在 5 到 500 之间")
        policy = {"keep_days": days, "keep_latest": runs}
        self.set_setting("storage_retention_policy", json.dumps(policy, ensure_ascii=False))
        return policy

    def create_hot_backup(self, dest_path: Path) -> int:
        """使用 SQLite 在线 backup API 生成一致性热备份（不阻塞读写，WAL 安全）。"""
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as src:
            dst = sqlite3.connect(str(dest_path), timeout=30.0)
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
        try:
            os.chmod(dest_path, 0o600)
        except OSError:
            pass
        return dest_path.stat().st_size

    def delete_project(self, project_id: str, *, managed_csv: Path | None = None) -> dict:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
            if not row:
                raise KeyError(f"项目不存在：{project_id}")
            proj = self._row_to_project(row)
            conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
        if managed_csv is not None:
            self.export_projects_csv(managed_csv)
        return proj

    def import_projects_csv(self, csv_text: str, *, mode: str = "upsert",
                            managed_csv: Path | None = None) -> dict:
        if mode not in ("upsert", "replace"):
            raise ValueError("mode 必须是 upsert 或 replace")
        reader = csv.DictReader(io.StringIO((csv_text or "").lstrip("\ufeff")))
        if not reader.fieldnames or "project_id" not in reader.fieldnames:
            raise ValueError("CSV 缺少必需表头列：project_id")
        parsed_rows: list[dict[str, str]] = []
        errors: list[dict] = []
        for idx, raw_row in enumerate(reader, start=2):
            pid = str(raw_row.get("project_id") or "").strip()
            if not pid or pid.startswith("#"):
                continue
            try:
                parsed_rows.append(self.validate_project_payload(raw_row))
            except ValueError as exc:
                errors.append({"line": idx, "project_id": pid, "reason": str(exc)})
        if not parsed_rows and errors:
            raise ValueError(f"导入失败：{errors[0]['reason']}（第 {errors[0]['line']} 行）")
        now = _now_iso()
        created, updated = 0, 0
        with self._lock, self._connect() as conn:
            if mode == "replace":
                conn.execute("DELETE FROM projects")
            for cleaned in parsed_rows:
                ex = conn.execute(
                    "SELECT project_id FROM projects WHERE project_id = ?", (cleaned["project_id"],)
                ).fetchone()
                if ex:
                    conn.execute(
                        """
                        UPDATE projects
                        SET project_name=?, department=?, owner=?, vcs_type=?, git_url=?, branch=?, status=?,
                            tech_stack=?, project_type=?, core_flows=?, scope_out=?, materials_dir=?,
                            weights=?, notes=?, updated_at=?
                        WHERE project_id=?
                        """,
                        (
                            cleaned["project_name"], cleaned["department"], cleaned["owner"],
                            cleaned["vcs_type"], cleaned["git_url"], cleaned["branch"], cleaned["status"],
                            cleaned["tech_stack"], cleaned["project_type"], cleaned["core_flows"],
                            cleaned["scope_out"], cleaned["materials_dir"], cleaned["weights"],
                            cleaned["notes"], now, cleaned["project_id"],
                        ),
                    )
                    updated += 1
                else:
                    conn.execute(
                        """
                        INSERT INTO projects (
                            project_id, project_name, department, owner, vcs_type, git_url, branch, status,
                            tech_stack, project_type, core_flows, scope_out, materials_dir, weights, notes,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cleaned["project_id"], cleaned["project_name"], cleaned["department"],
                            cleaned["owner"], cleaned["vcs_type"], cleaned["git_url"], cleaned["branch"], cleaned["status"],
                            cleaned["tech_stack"], cleaned["project_type"], cleaned["core_flows"],
                            cleaned["scope_out"], cleaned["materials_dir"], cleaned["weights"],
                            cleaned["notes"], now, now,
                        ),
                    )
                    created += 1
        if managed_csv is not None:
            self.export_projects_csv(managed_csv)
        return {"created": created, "updated": updated, "errors": errors}

    def update_project_metrics(self, project_id: str, *, loc_total: int | None,
                               file_count: int | None, languages: dict[str, int] | None,
                               last_commit_at: str | None, last_commit_author: str = "",
                               last_commit_subject: str = "") -> None:
        now = _now_iso()
        lang_json = json.dumps(languages or {}, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE projects
                SET loc_total = COALESCE(?, loc_total),
                    file_count = COALESCE(?, file_count),
                    languages_json = CASE WHEN ? != '{}' THEN ? ELSE languages_json END,
                    last_commit_at = COALESCE(?, last_commit_at),
                    last_commit_author = CASE WHEN ? != '' THEN ? ELSE last_commit_author END,
                    last_commit_subject = CASE WHEN ? != '' THEN ? ELSE last_commit_subject END,
                    metrics_updated_at = ?
                WHERE project_id = ?
                """,
                (
                    loc_total, file_count, lang_json, lang_json, last_commit_at,
                    last_commit_author, last_commit_author, last_commit_subject, last_commit_subject,
                    now, project_id,
                ),
            )

    def update_project_git_check(self, project_id: str, *, ok: bool | None, error: str = "") -> None:
        now = _now_iso()
        val = None if ok is None else (1 if ok else 0)
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE projects SET git_check_ok = ?, git_check_error = ?, git_checked_at = ? WHERE project_id = ?",
                (val, (error or "")[:300], now, project_id),
            )

    # ---------- 提示词模板版本管理 ----------

    @staticmethod
    def _sync_prompt_file(prompts_dir: Path | None, key: str, content: str | None) -> None:
        if prompts_dir is None:
            return
        fname = PROMPT_FILENAMES.get(key)
        if not fname:
            return
        target = prompts_dir / fname
        if content is None:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return
        prompts_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(prompts_dir, 0o755)
        except OSError:
            pass
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        try:
            # Scanner 容器以非 root node 运行，需允许组/其他只读
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        os.replace(tmp, target)

    def get_active_prompt(self, prompt_key: str) -> dict | None:
        if prompt_key not in PROMPT_KEYS:
            raise ValueError(f"不支持的提示词键：{prompt_key}")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM prompt_versions WHERE prompt_key = ? AND is_active = 1 ORDER BY version DESC LIMIT 1",
                (prompt_key,),
            ).fetchone()
            return dict(row) if row else None

    def list_prompt_versions(self, prompt_key: str, limit: int = 20) -> list[dict]:
        if prompt_key not in PROMPT_KEYS:
            raise ValueError(f"不支持的提示词键：{prompt_key}")
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, prompt_key, version, note, updated_by, is_active, created_at, length(content) AS char_count "
                "FROM prompt_versions WHERE prompt_key = ? ORDER BY version DESC LIMIT ?",
                (prompt_key, max(1, min(100, int(limit)))),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_prompt(self, prompt_key: str, content: str, *, updated_by: str,
                    note: str = "", prompts_dir: Path | None = None) -> dict:
        if prompt_key not in PROMPT_KEYS:
            raise ValueError(f"不支持的提示词键：{prompt_key}")
        text = str(content or "")
        if prompt_key == "skill_md" and len(text.strip()) < 200:
            raise ValueError("SKILL.md 核心提示词内容过短（至少 200 字符），请勿误清空")
        now = _now_iso()
        with self._lock, self._connect() as conn:
            max_ver = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM prompt_versions WHERE prompt_key = ?",
                (prompt_key,),
            ).fetchone()[0]
            next_ver = int(max_ver) + 1
            conn.execute("UPDATE prompt_versions SET is_active = 0 WHERE prompt_key = ?", (prompt_key,))
            cur = conn.execute(
                """
                INSERT INTO prompt_versions (prompt_key, version, content, note, updated_by, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (prompt_key, next_ver, text, (note or "").strip()[:200], updated_by, now),
            )
            row = conn.execute("SELECT * FROM prompt_versions WHERE id = ?", (cur.lastrowid,)).fetchone()
            assert row is not None
            saved = dict(row)
        self._sync_prompt_file(prompts_dir, prompt_key, text if text.strip() else None)
        return saved

    def rollback_prompt(self, prompt_key: str, version_id: int, *, updated_by: str,
                        prompts_dir: Path | None = None) -> dict:
        if prompt_key not in PROMPT_KEYS:
            raise ValueError(f"不支持的提示词键：{prompt_key}")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM prompt_versions WHERE id = ? AND prompt_key = ?",
                (version_id, prompt_key),
            ).fetchone()
            if not row:
                raise KeyError("未找到目标提示词历史版本")
            content = row["content"]
            src_ver = row["version"]
        return self.save_prompt(
            prompt_key,
            content,
            updated_by=updated_by,
            note=f"回滚至 v{src_ver}",
            prompts_dir=prompts_dir,
        )

    def reset_prompt(self, prompt_key: str, *, prompts_dir: Path | None = None) -> None:
        """恢复内置默认提示词（取消数据库自定义覆盖并删除热挂载文件）。"""
        if prompt_key not in PROMPT_KEYS:
            raise ValueError(f"不支持的提示词键：{prompt_key}")
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE prompt_versions SET is_active = 0 WHERE prompt_key = ?", (prompt_key,))
        self._sync_prompt_file(prompts_dir, prompt_key, None)

    def sync_all_active_prompts(self, prompts_dir: Path) -> None:
        for key in PROMPT_KEYS:
            active = self.get_active_prompt(key)
            if active and str(active.get("content") or "").strip():
                self._sync_prompt_file(prompts_dir, key, str(active["content"]))
            else:
                self._sync_prompt_file(prompts_dir, key, None)

    # ---------- Git 仓库账号凭据管理（仅超管） ----------

    @staticmethod
    def _public_git_cred(row: sqlite3.Row, include_secret: bool = False) -> dict:
        out = {
            "id": int(row["id"]),
            "label": row["label"] or "",
            "protocol": row["protocol"] or "https",
            "host": row["host"],
            "path_prefix": row["path_prefix"] or "",
            "username": row["username"],
            "has_secret": bool(row["secret"]),
            "secret_masked": mask_secret(row["secret"] or ""),
            "is_active": bool(row["is_active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_secret:
            out["secret"] = row["secret"] or ""
        return out

    def list_git_credentials(self, include_secret: bool = False) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM git_credentials ORDER BY id ASC").fetchall()
            return [self._public_git_cred(r, include_secret=include_secret) for r in rows]

    def upsert_git_credential(self, data: dict, *, cred_id: int | None = None,
                              runtime_file: Path | None = None,
                              base_file: Path | None = None) -> dict:
        protocol = str(data.get("protocol") or "https").strip().lower()
        if protocol not in ("https", "http", "svn"):
            raise ValueError("协议仅支持 https、http 或 svn")
        host = (
            str(data.get("host") or "").strip().lower()
            .removeprefix("https://").removeprefix("http://").removeprefix("svn://")
            .strip("/")
        )
        if not host or any(ch.isspace() or ch in "@/" for ch in host):
            raise ValueError("Git 域名/地址格式无效，例如 git.shineyue.com 或 10.0.1.10:8080")
        path_prefix = str(data.get("path_prefix") or "").strip()
        if path_prefix and not path_prefix.startswith("/"):
            path_prefix = "/" + path_prefix
        if any(ch.isspace() for ch in path_prefix):
            raise ValueError("路径前缀不能包含空白字符")
        username = str(data.get("username") or "").strip()
        if not username or any(ch in "\r\n" for ch in username):
            raise ValueError("Git 用户名不能为空")
        secret_input = str(data.get("secret") or "")
        if any(ch in "\r\n" for ch in secret_input):
            raise ValueError("Git 密码/Token 不能包含换行符")
        label = str(data.get("label") or "").strip()[:100]
        is_active = 1 if bool(data.get("is_active", True)) else 0
        now = _now_iso()

        with self._lock, self._connect() as conn:
            if cred_id is not None:
                old = conn.execute("SELECT * FROM git_credentials WHERE id = ?", (cred_id,)).fetchone()
                if not old:
                    raise KeyError("未找到该 Git 账号配置")
                final_secret = secret_input if secret_input else old["secret"]
                if not final_secret:
                    raise ValueError("密码/访问令牌不能为空")
                conn.execute(
                    """
                    UPDATE git_credentials
                    SET label=?, protocol=?, host=?, path_prefix=?, username=?, secret=?, is_active=?, updated_at=?
                    WHERE id=?
                    """,
                    (label, protocol, host, path_prefix, username, final_secret, is_active, now, cred_id),
                )
                row = conn.execute("SELECT * FROM git_credentials WHERE id = ?", (cred_id,)).fetchone()
            else:
                if not secret_input:
                    raise ValueError("新增 Git 账号时密码/访问令牌不能为空")
                cur = conn.execute(
                    """
                    INSERT INTO git_credentials (label, protocol, host, path_prefix, username, secret, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (label, protocol, host, path_prefix, username, secret_input, is_active, now, now),
                )
                row = conn.execute("SELECT * FROM git_credentials WHERE id = ?", (cur.lastrowid,)).fetchone()
            assert row is not None
            saved = self._public_git_cred(row)
        if runtime_file is not None:
            self.export_git_credentials_file(runtime_file, base_file=base_file)
        return saved

    def delete_git_credential(self, cred_id: int, *, runtime_file: Path | None = None,
                              base_file: Path | None = None) -> dict:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM git_credentials WHERE id = ?", (cred_id,)).fetchone()
            if not row:
                raise KeyError("未找到该 Git 账号配置")
            deleted = self._public_git_cred(row)
            conn.execute("DELETE FROM git_credentials WHERE id = ?", (cred_id,))
        if runtime_file is not None:
            self.export_git_credentials_file(runtime_file, base_file=base_file)
        return deleted

    def export_git_credentials_file(self, runtime_file: Path, base_file: Path | None = None) -> int:
        """将超管维护的 Git 凭据与宿主机只读 git-credentials 合并导出为 0600 凭据文件。"""
        active_creds = [c for c in self.list_git_credentials(include_secret=True) if c["is_active"]]
        lines: list[str] = []
        managed_keys: set[tuple[str, str, str]] = set()
        for c in active_creds:
            u = urllib.parse.quote(str(c["username"]), safe="")
            s = urllib.parse.quote(str(c["secret"]), safe="")
            lines.append(f"{c['protocol']}://{u}:{s}@{c['host']}{c['path_prefix']}")
            managed_keys.add((c["protocol"], c["host"].lower(), c["path_prefix"]))

        # 合并只读挂载的初始凭据中未被 Web 覆盖的条目，确保兼容存量部署
        if base_file and base_file.is_file():
            try:
                for raw in base_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        p = urllib.parse.urlparse(line)
                        key = (p.scheme.lower(), (p.hostname or "").lower() + (f":{p.port}" if p.port else ""), p.path or "")
                    except ValueError:
                        key = ("", "", "")
                    if key not in managed_keys:
                        lines.append(line)
            except OSError:
                pass

        if not lines:
            try:
                runtime_file.unlink(missing_ok=True)
            except OSError:
                pass
            return 0
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = runtime_file.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, runtime_file)
        return len(lines)

    # ---------- SMTP 配置与邮件投递日志 ----------

    def get_smtp_config(self, include_password: bool = False) -> dict:
        defaults = {
            "enabled": False,
            "host": "",
            "port": 465,
            "security": "ssl",
            "username": "",
            "password": "",
            "from_email": "",
            "from_name": "八性代码质量审查平台",
            "portal_url": "",
            "attachment_format": "docx",
            "max_attach_mb": 15,
            "auto_send_on_batch": True,
            "summary_cc_emails": "",
        }
        stored = self.get_setting("smtp_config")
        if isinstance(stored, dict):
            defaults.update({k: v for k, v in stored.items() if k in defaults})
        out = dict(defaults)
        pwd = str(out.get("password") or "")
        out["password_set"] = bool(pwd)
        out["password_masked"] = mask_secret(pwd)
        if not include_password:
            out.pop("password", None)
        return out

    def save_smtp_config(self, payload: dict) -> dict:
        cur = self.get_smtp_config(include_password=True)
        enabled = bool(payload.get("enabled", cur["enabled"]))
        host = str(payload.get("host", cur["host"]) or "").strip()
        try:
            port = int(payload.get("port", cur["port"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("SMTP 端口必须为整数") from exc
        if port < 1 or port > 65535:
            raise ValueError("SMTP 端口需在 1–65535 之间")
        security = str(payload.get("security", cur["security"]) or "ssl").strip().lower()
        if security not in ("ssl", "starttls", "none"):
            raise ValueError("加密方式仅支持 ssl / starttls / none")
        username = str(payload.get("username", cur["username"]) or "").strip()
        raw_pwd = str(payload.get("password") or "")
        if payload.get("clear_password"):
            password = ""
        elif raw_pwd:
            password = raw_pwd
        else:
            password = str(cur.get("password") or "")
        from_email = str(payload.get("from_email", cur["from_email"]) or "").strip() or (username if EMAIL_RE.match(username) else "")
        if from_email and not EMAIL_RE.match(from_email):
            raise ValueError(f"发件人邮箱格式无效：{from_email}")
        from_name = str(payload.get("from_name", cur["from_name"]) or "八性代码质量审查平台").strip()[:80]
        portal_url = str(payload.get("portal_url", cur["portal_url"]) or "").strip().rstrip("/")
        if portal_url and not portal_url.lower().startswith(("http://", "https://")):
            raise ValueError("平台访问地址需以 http:// 或 https:// 开头")
        att_fmt = str(payload.get("attachment_format", cur["attachment_format"]) or "docx").strip().lower()
        if att_fmt not in ("docx", "md", "both"):
            raise ValueError("附件格式仅支持 docx / md / both")
        try:
            max_mb = int(payload.get("max_attach_mb", cur["max_attach_mb"]))
        except (TypeError, ValueError):
            max_mb = 15
        max_mb = max(1, min(50, max_mb))
        auto_send = bool(payload.get("auto_send_on_batch", cur["auto_send_on_batch"]))
        raw_cc = str(payload.get("summary_cc_emails", cur["summary_cc_emails"]) or "").strip()
        cc_list = [e.strip() for e in re.split(r"[;,，、\s]+", raw_cc) if e.strip()]
        for cc in cc_list:
            if not EMAIL_RE.match(cc):
                raise ValueError(f"管理层汇总抄送邮箱格式无效：{cc}")
        if enabled and not host:
            raise ValueError("启用邮件投递时必须填写 SMTP 服务器地址")
        saved = {
            "enabled": enabled,
            "host": host,
            "port": port,
            "security": security,
            "username": username,
            "password": password,
            "from_email": from_email,
            "from_name": from_name,
            "portal_url": portal_url,
            "attachment_format": att_fmt,
            "max_attach_mb": max_mb,
            "auto_send_on_batch": auto_send,
            "summary_cc_emails": ", ".join(cc_list),
        }
        self.set_setting("smtp_config", saved)
        return self.get_smtp_config(include_password=False)

    def record_email_log(self, *, run_date: str, run_id: str, recipient_email: str,
                         recipient_name: str, recipient_type: str, project_ids: list[str],
                         subject: str, attachment_names: list[str], status: str, error: str = "") -> dict:
        now = _now_iso()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO email_logs (
                    run_date, run_id, recipient_email, recipient_name, recipient_type,
                    project_ids_json, subject, attachment_names_json, status, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_date, run_id, recipient_email, recipient_name[:80], recipient_type,
                    json.dumps(project_ids, ensure_ascii=False), subject[:300],
                    json.dumps(attachment_names, ensure_ascii=False), status, (error or "")[:500], now,
                ),
            )
            row = conn.execute("SELECT * FROM email_logs WHERE id = ?", (cur.lastrowid,)).fetchone()
            assert row is not None
            return self._row_to_email_log(row)

    @staticmethod
    def _row_to_email_log(row: sqlite3.Row) -> dict:
        try:
            pids = json.loads(row["project_ids_json"] or "[]")
        except ValueError:
            pids = []
        try:
            atts = json.loads(row["attachment_names_json"] or "[]")
        except ValueError:
            atts = []
        return {
            "id": int(row["id"]),
            "run_date": row["run_date"],
            "run_id": row["run_id"],
            "recipient_email": row["recipient_email"],
            "recipient_name": row["recipient_name"],
            "recipient_type": row["recipient_type"],
            "project_ids": pids if isinstance(pids, list) else [],
            "subject": row["subject"],
            "attachment_names": atts if isinstance(atts, list) else [],
            "status": row["status"],
            "error": row["error"],
            "created_at": row["created_at"],
        }

    def list_email_logs(self, limit: int = 150) -> list[dict]:
        lim = max(1, min(500, int(limit or 150)))
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM email_logs ORDER BY id DESC LIMIT ?", (lim,)).fetchall()
            return [self._row_to_email_log(r) for r in rows]

    def get_email_log(self, log_id: int) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM email_logs WHERE id = ?", (log_id,)).fetchone()
            return self._row_to_email_log(row) if row else None


def mask_secret(value: str) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    if len(s) <= 4:
        return "****"
    if len(s) <= 8:
        return s[:1] + "****" + s[-2:]
    return s[:3] + "****" + s[-4:]


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path or not path.is_file():
        return out
    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k:
                out[k] = v.strip()
    except OSError:
        pass
    return out


def write_env_file(path: Path, data: dict[str, str]) -> None:
    lines = ["# Managed by AI Code Audit Portal (superadmin) — do not commit"]
    for k, v in data.items():
        key = str(k).strip()
        val = str(v).strip()
        if not ENV_KEY_RE.match(key):
            raise ValueError(f"环境变量名格式无效：{key}")
        if "\n" in val or "\r" in val:
            raise ValueError(f"环境变量 {key} 的值不能包含换行符")
        if val != "":
            lines.append(f"{key}={val}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def test_openai_endpoint(base_url: str, model: str, api_key: str, timeout_sec: int = 15) -> dict:
    """测试 OpenAI 兼容模型接口连通性与响应耗时（不泄露密钥）。"""
    url_root = (base_url or "").strip().rstrip("/")
    if not url_root.lower().startswith(("http://", "https://")):
        raise ValueError("模型接口地址必须以 http:// 或 https:// 开头")
    target = f"{url_root}/chat/completions"
    payload = {
        "model": (model or "").strip(),
        "messages": [{"role": "user", "content": "Reply with OK only."}],
        "max_tokens": 16,
        "temperature": 0,
    }
    headers = {"Content-Type": "application/json"}
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    req = urllib.request.Request(
        target, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            elapsed_ms = round((time.perf_counter() - t0) * 1000)
            parsed = json.loads(raw) if raw else {}
            choices = parsed.get("choices") or []
            msg = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
            content = str(msg.get("content") or "").strip()[:120]
            reasoning = msg.get("reasoning_content") or msg.get("reasoning")
            return {
                "ok": True,
                "status_code": resp.status,
                "elapsed_ms": elapsed_ms,
                "model": parsed.get("model") or model,
                "reply_preview": content,
                "has_thinking": bool(reasoning),
                "error": "",
            }
    except urllib.error.HTTPError as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        try:
            body = exc.read().decode("utf-8", errors="replace")[:300]
        finally:
            exc.close()
        return {
            "ok": False,
            "status_code": exc.code,
            "elapsed_ms": elapsed_ms,
            "model": model,
            "reply_preview": "",
            "has_thinking": False,
            "error": f"HTTP {exc.code}: {body}",
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        return {
            "ok": False,
            "status_code": 0,
            "elapsed_ms": elapsed_ms,
            "model": model,
            "reply_preview": "",
            "has_thinking": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def compute_issue_fingerprint(item_id: str, title: str, evidence_text: str = "") -> str:
    """为报告中的风险问题生成跨批次稳定指纹（忽略行号偏移，便于持续匹配豁免与整改状态）。"""
    norm_item = (item_id or "").strip().lower()
    norm_title = re.sub(r"\s+", " ", (title or "").strip().lower())
    # 提取证据中的文件路径部分并去掉 :line-line 行号偏移
    ev = (evidence_text or "").strip().lower()
    ev = re.sub(r":\d+(?:-\d+)?", "", ev)
    ev = re.sub(r"\s+", " ", ev)[:160]
    raw = f"{norm_item}|{norm_title}|{ev}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def validate_git_url(raw_url: str, vcs_type: str = "git") -> str:
    """校验 Git / SVN 仓库地址，防止参数注入与 file:// 本地路径泄露。"""
    return vcs.validate_repo_url(raw_url, vcs_type=vcs_type)


def probe_git_remote(git_url: str, branch: str = "", timeout_sec: int = 15,
                     credential_file: Path | None = None,
                     vcs_type: str = "") -> dict:
    """执行只读远端探测：Git 走 git ls-remote，SVN 走 svn info --xml。"""
    resolved_vcs = vcs.detect_vcs_type(git_url, vcs_type or None)
    if resolved_vcs == "svn":
        return vcs.probe_svn_remote(
            git_url, branch=branch, timeout_sec=timeout_sec, credential_file=credential_file
        )
    url = validate_git_url(git_url, vcs_type="git")
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if credential_file and credential_file.is_file():
        env["AUDIT_GIT_CREDENTIAL_FILE"] = str(credential_file)
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--heads", "--symref", url],
            capture_output=True, text=True, timeout=timeout_sec, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "vcs_type": "git", "branches": [], "default_branch": "", "branch_exists": False, "error": f"连接 Git 仓库超时（>{timeout_sec}s）"}
    except OSError as exc:
        return {"ok": False, "vcs_type": "git", "branches": [], "default_branch": "", "branch_exists": False, "error": f"无法调用 git：{exc}"}
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "git ls-remote 失败").strip().splitlines()
        return {"ok": False, "vcs_type": "git", "branches": [], "default_branch": "", "branch_exists": False, "error": " ".join(err[:2])[:300]}

    branches: list[str] = []
    default_branch = ""
    for line in proc.stdout.splitlines():
        if line.startswith("ref: refs/heads/"):
            default_branch = line[len("ref: refs/heads/"):].split("\t", 1)[0].strip()
        elif "\trefs/heads/" in line:
            b = line.split("\trefs/heads/", 1)[1].strip()
            if b:
                branches.append(b)
    branches = sorted(set(branches))
    target_branch = (branch or "").strip()
    return {
        "ok": True,
        "vcs_type": "git",
        "branches": branches,
        "default_branch": default_branch or ("main" if "main" in branches else ("master" if "master" in branches else (branches[0] if branches else ""))),
        "branch_exists": (target_branch in branches) if target_branch else bool(branches),
        "error": "",
    }


def collect_repo_metrics(repo_dir: Path, cloc_json_path: Path | None = None) -> dict:
    """零依赖快速采集本地 Git / SVN 仓库的代码行数、语言分布与最后提交时间。"""
    out: dict = {
        "loc_total": None,
        "file_count": None,
        "languages": {},
        "last_commit_at": None,
        "last_commit_author": "",
        "last_commit_subject": "",
    }
    if not repo_dir.is_dir():
        return out

    is_svn = vcs.is_svn_working_copy(repo_dir)
    # 1. 最后代码提交时间 / 作者 / 主题
    if is_svn:
        svn_info = vcs.svn_working_copy_info(repo_dir)
        out["last_commit_at"] = svn_info.get("last_commit_at")
        out["last_commit_author"] = svn_info.get("last_commit_author") or ""
        out["last_commit_subject"] = svn_info.get("last_commit_subject") or ""
    else:
        try:
            proc = subprocess.run(
                ["git", "log", "-1", "--format=%cI\x1f%an\x1f%s"],
                cwd=str(repo_dir), capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                parts = proc.stdout.strip().split("\x1f", 2)
                out["last_commit_at"] = parts[0].strip() or None
                if len(parts) > 1:
                    out["last_commit_author"] = parts[1].strip()[:80]
                if len(parts) > 2:
                    out["last_commit_subject"] = parts[2].strip()[:140]
        except (OSError, subprocess.TimeoutExpired):
            pass

    # 2. 代码行数与语言分布：优先读 cloc.json，否则快速遍历受控文件
    if cloc_json_path and cloc_json_path.is_file():
        try:
            cloc_data = json.loads(cloc_json_path.read_text(encoding="utf-8"))
            if isinstance(cloc_data, dict):
                s = cloc_data.get("SUM")
                if isinstance(s, dict):
                    out["loc_total"] = int(s.get("code") or 0)
                    out["file_count"] = int(s.get("nFiles") or 0)
                langs: dict[str, int] = {}
                for k, v in cloc_data.items():
                    if k in ("header", "SUM") or not isinstance(v, dict):
                        continue
                    code = int(v.get("code") or 0)
                    if code > 0:
                        langs[str(k)] = code
                if langs:
                    top = sorted(langs.items(), key=lambda kv: kv[1], reverse=True)[:8]
                    out["languages"] = dict(top)
                if out["loc_total"] is not None:
                    return out
        except (OSError, ValueError, TypeError):
            pass

    try:
        if is_svn:
            rel_paths = vcs.list_working_copy_files(repo_dir)
        else:
            ls = subprocess.run(
                ["git", "ls-files", "-z"], cwd=str(repo_dir), capture_output=True, timeout=12,
            )
            if ls.returncode == 0:
                rel_paths = [p.decode("utf-8", errors="ignore") for p in ls.stdout.split(b"\x00") if p]
            else:
                rel_paths = []
    except (OSError, subprocess.TimeoutExpired):
        rel_paths = []

    if rel_paths:
        total_loc = 0
        total_files = 0
        lang_counts: dict[str, int] = {}
        for rel in rel_paths:
            parts = Path(rel).parts
            if any(pt in _SKIP_DIR_PARTS for pt in parts):
                continue
            ext = Path(rel).suffix.lower()
            lang = _EXT_LANG_MAP.get(ext)
            if not lang:
                continue
            fpath = repo_dir / rel
            try:
                st = fpath.stat()
                if st.st_size > 1_048_576:
                    continue
                raw = fpath.read_bytes()
                if b"\x00" in raw[:1024]:
                    continue
                loc = sum(1 for line in raw.splitlines() if line.strip())
            except OSError:
                continue
            total_files += 1
            total_loc += loc
            lang_counts[lang] = lang_counts.get(lang, 0) + loc
        out["loc_total"] = total_loc
        out["file_count"] = total_files
        top = sorted(lang_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
        out["languages"] = dict(top)
    return out


def compute_stale_info(last_commit_at: str | None, warn_days: int = 90,
                       critical_days: int = 180, now: dt.datetime | None = None) -> tuple[int | None, str]:
    """根据最后代码提交时间计算未维护天数及预警级别（active / warning / critical / unknown）。"""
    if not last_commit_at:
        return None, "unknown"
    try:
        ts = dt.datetime.fromisoformat(str(last_commit_at).strip().replace("Z", "+00:00"))
    except ValueError:
        return None, "unknown"
    ref = now or dt.datetime.now(ts.tzinfo or dt.timezone.utc)
    if ts.tzinfo is None and ref.tzinfo is not None:
        ref = ref.replace(tzinfo=None)
    elif ts.tzinfo is not None and ref.tzinfo is None:
        ref = ref.replace(tzinfo=ts.tzinfo)
    days = max(0, int((ref - ts).total_seconds() // 86400))
    if days >= critical_days:
        return days, "critical"
    if days >= warn_days:
        return days, "warning"
    return days, "active"
