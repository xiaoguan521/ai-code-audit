"""管理端数据底座（零外部依赖）：SQLite (WAL) 用户体系、RBAC 权限、会话、操作审计日志与中文姓名转全拼。

持久化位置：`${output_dir}/state/platform.db`（权限 0600）。
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from pathlib import Path

ROLES = ("superadmin", "admin", "user")
EMAIL_NOTIFY_MODES = ("batch", "risk_only", "off")
SESSION_TTL_SEC = 24 * 3600
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,64}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

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


def user_owns_project(user: dict | None, project: dict) -> bool:
    """判断用户是否有权访问该项目（superadmin/admin 全部可见；普通用户按负责人匹配）。"""
    if not user:
        return False
    role = user.get("role")
    if role in ("superadmin", "admin"):
        return True
    owners = split_owners(project.get("owner"))
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
                """
            )

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
