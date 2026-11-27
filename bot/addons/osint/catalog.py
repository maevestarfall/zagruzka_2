"""Curated OSINT resource catalog.

Auto-derived from the public Awesome-AI-OSINT repository
(https://github.com/ubikron/Awesome-AI-OSINT) and grouped into
person-research categories. Pure data — no imports, no side effects.
"""

from __future__ import annotations

SOURCE_URL = "https://github.com/ubikron/Awesome-AI-OSINT"

# key -> {"label", "emoji", "items": [[name, url], ...]}
CATALOG: dict[str, dict] = {
    "face": {
        "label": "Поиск по лицу / фото",
        "emoji": "👤",
        "items": [
            [
                "FaceSeek AI Powered Reverse Face Search",
                "https://www.faceseek.online/"
            ],
            [
                "Lenso.ai AI Face Search",
                "https://lenso.ai/en/page/face-search"
            ],
            [
                "ProFace Finder AI Face Comprasion",
                "https://profacefinder.com/face-comparison/"
            ],
            [
                "Raugen AI Face Comprasion",
                "https://raugen.com/ai-tools/face-comparison"
            ],
            [
                "The Flux Train AI Face Similarity Score",
                "https://thefluxtrain.com/tools/ai-face-score"
            ],
            [
                "ChatGPT based ethnicity guessr",
                "https://chatgpt.com/g/g-ckfCaNtJt-ethnicity-guesser"
            ],
            [
                "Ethnicity Analysis",
                "https://justbuildthings.com/ai-image-analysis/ethnicity-analysis"
            ],
            [
                "Galaxy AI Face Analyzer",
                "https://image.galaxy.ai/ai-face-analyzer"
            ]
        ]
    },
    "username": {
        "label": "Поиск по нику",
        "emoji": "🆔",
        "items": [
            [
                "Aliens eye",
                "https://github.com/arxhr007/Aliens_eye"
            ]
        ]
    },
    "contacts": {
        "label": "Контакты и лиды",
        "emoji": "📞",
        "items": [
            [
                "Neuralead",
                "https://neuralead.ai/"
            ],
            [
                "Prospectrin",
                "https://prospectrin.com/"
            ],
            [
                "Expose Team",
                "https://expose.team?utm_source=github.com&utm_campaign=ubikron_Awesome_AI_OSINT"
            ]
        ]
    },
    "geoint": {
        "label": "Геолокация по фото (GEOINT)",
        "emoji": "🌍",
        "items": [
            [
                "GeoSpy",
                "https://geospy.ai/"
            ],
            [
                "Picarta",
                "https://picarta.ai/"
            ],
            [
                "FindLocation",
                "https://findpiclocation.com/"
            ],
            [
                "GEOGPT Free",
                "https://www.yeschat.ai/gpts-9t55QZdKjT7-GeoGPT"
            ],
            [
                "GeoFinder",
                "https://geofinderai.com/"
            ],
            [
                "EarthKit",
                "https://earthkit.app/"
            ],
            [
                "Planet: AI sattelite object detection",
                "https://www.planet.com/products/"
            ],
            [
                "Geointel Python package",
                "https://github.com/atiilla/GeoIntel"
            ]
        ]
    },
    "image": {
        "label": "Поиск по картинке",
        "emoji": "🖼",
        "items": [
            [
                "Lenso.ai: reverse image search tool",
                "https://lenso.ai/en"
            ],
            [
                "Vehicle Identifier",
                "https://vehicle-ai.vercel.app/"
            ]
        ]
    },
    "dorks": {
        "label": "Google Dorks (AI)",
        "emoji": "🔎",
        "items": [
            [
                "The Dorker",
                "https://www.yeschat.ai/ru/gpts-2OToO2th4j-The-Dorker"
            ],
            [
                "Advanced Dorks Generator",
                "https://syntax.goldenowl.ai/"
            ]
        ]
    },
    "net": {
        "label": "IP / домены / поддомены",
        "emoji": "🌐",
        "items": [
            [
                "ZoomEye GPT",
                "https://www.zoomeye.ai/gpt"
            ],
            [
                "CensysGPT",
                "https://gpt.censys.io/"
            ],
            [
                "ShodanGPT",
                "https://chatgpt.com/g/g-JwmZilzME-shodan-gpt"
            ],
            [
                "God Eye",
                "https://github.com/Vyntral/god-eye"
            ],
            [
                "ReconBorne",
                "https://github.com/vylarion/reconborne"
            ],
            [
                "AISubs",
                "https://github.com/topscoder/aisubs"
            ],
            [
                "SmartSubAI",
                "https://github.com/OCEANOFANYTHING/SmartSubAI"
            ]
        ]
    },
    "assistants": {
        "label": "AI-ассистенты OSINT",
        "emoji": "🧠",
        "items": [
            [
                "OSINT GPT",
                "https://chatgpt.com/g/g-ysjJG1VjM-osint-gpt"
            ],
            [
                "AI Assistant by Cyclect.io",
                "https://chatgpt.com/g/g-aZQ1x6vqB-ai-osint"
            ],
            [
                "OSInt GPT",
                "https://chatgpt.com/g/g-CaB1umlDg-osint-gpt"
            ],
            [
                "Analyst's Co-Pilot-AI-powered intelligence analysis tool",
                "https://theee.ai/tools/analyst-s-co-pilot-2OToA2Irnq"
            ],
            [
                "OSINTAI",
                "https://chatgpt.com/g/g-bTltYNKUJ-osint-ai"
            ],
            [
                "ubikron.com",
                "https://ubikron.com"
            ],
            [
                "Ubikron",
                "https://www.ubikron.com/"
            ],
            [
                "Taranis AI",
                "https://github.com/taranis-ai/taranis-ai"
            ],
            [
                "Cyclect: Ultimate AI OSINT Search Engine",
                "https://cylect.io/"
            ],
            [
                "Research Pilot v2.5",
                "https://digitaldigging.org/research/"
            ],
            [
                "OSINTGPT",
                "https://github.com/estebanpdl/osintgpt"
            ],
            [
                "Robin:AI-Powered Dark Web OSINT Tool",
                "https://github.com/apurvsinghgautam/robin"
            ],
            [
                "Perplexity Sonar OSINT Assistant",
                "https://github.com/AXRoux/OSINT-Assistant"
            ],
            [
                "DarkGPT",
                "https://github.com/binaco/DarkGPT"
            ],
            [
                "Maigret LLM",
                "https://github.com/BurtTheCoder/mcp-maigret"
            ],
            [
                "Sherlock Mail",
                "https://github.com/Hamed233/Sherlock-Mail-AI-Powered-Email-Intelligence"
            ],
            [
                "OSINT AI CLI",
                "https://github.com/Mawgaming/osint-ai-cli"
            ],
            [
                "AI OSINT Security Analyzer",
                "https://github.com/Armaan29-09-2005/AI-OSINT-Security-Analyzer"
            ],
            [
                "ArkhamMirror",
                "https://github.com/1n3202/ArkhamMirror"
            ],
            [
                "GeoSentinel 2.0",
                "https://github.com/acuestamd/geosentinel"
            ],
            [
                "PANO",
                "https://github.com/ALW1EZ/PANO"
            ],
            [
                "Ubikron Advanced Enrichments",
                "https://github.com/ubikron/Advanced-Enrichments"
            ],
            [
                "Awesome OSINT Chrome Extensions",
                "https://github.com/ubikron/awesome-osint-chrome-extensions"
            ],
            [
                "OSINT People",
                "https://github.com/ubikron/OSINT-People"
            ],
            [
                "OSINT Companies",
                "https://github.com/ubikron/OSINT-Companies"
            ],
            [
                "OSINT Newsletters",
                "https://github.com/ubikron/OSINT-newsletters"
            ],
            [
                "OSINT Books",
                "https://github.com/ubikron/OSINT-Books"
            ],
            [
                "OSINT Conferences",
                "https://github.com/ubikron/OSINT-Conferences"
            ],
            [
                "OSINT CTFs",
                "https://github.com/ubikron/OSINT-CTFs"
            ],
            [
                "Linkedin",
                "https://www.linkedin.com/company/ubikron/posts/?feedView=all"
            ],
            [
                "YouTube",
                "https://www.youtube.com/watch?v=inLkAmcAgjw"
            ]
        ]
    },
    "events": {
        "label": "Мониторинг событий в мире",
        "emoji": "📡",
        "items": [
            [
                "Wordmonitor",
                "https://www.worldmonitor.app/"
            ],
            [
                "IranWarMap",
                "https://iranwarmap.com/"
            ],
            [
                "GlobalThreatMap",
                "https://github.com/unicodeveloper/globalthreatmap"
            ]
        ]
    },
    "detect": {
        "label": "Детекция AI / дипфейков",
        "emoji": "🛡",
        "items": [
            [
                "ImageWhisperer",
                "https://imagewhisperer.org/"
            ],
            [
                "How to detect deepfakes with AI",
                "https://www.digitaldigging.org/p/how-to-detect-ai-deepfakes-with-ai"
            ],
            [
                "AI Detector",
                "https://aidetector.com/"
            ],
            [
                "Sensity.ai tool",
                "https://sensity.ai/"
            ],
            [
                "DeepWare",
                "https://deepware.ai/"
            ]
        ]
    },
    "skills": {
        "label": "Скиллы и шаблоны (Claude/OpenClaw)",
        "emoji": "🧩",
        "items": [
            [
                "Claude OSINT Investigator: Claude code template",
                "https://github.com/danielrosehill/Claude-OSINT-Investigator"
            ],
            [
                "OSINT Graph Analyzer",
                "https://openclawdir.com/skills/osint-graph-analyzer-3lr21x"
            ],
            [
                "Xint - X intelligence",
                "https://clawhub.ai/0xnyk/xint"
            ],
            [
                "Ocas Scout",
                "https://clawhub.ai/indigokarasu/ocas-scout"
            ],
            [
                "Multi Search Engine",
                "https://clawhub.ai/gpyangyoujun/multi-search-engine"
            ],
            [
                "Find People",
                "https://openclawdir.com/skills/find-people-uf0ugu"
            ],
            [
                "OSINT Investigator",
                "https://clawskills.sh/skills/cineglobe-osint-investigator"
            ],
            [
                "OSINT Human Research",
                "https://clawskills.sh/skills/andrewchen-oss-nuwa-world-osint-human-research"
            ],
            [
                "OSINT skill for Claude Code, OpenClaw, Codex, OpenCode",
                "https://github.com/smixs/osint-skill"
            ],
            [
                "OSINT Tools MCP Server",
                "https://github.com/frishtik/osint-tools-mcp-server"
            ]
        ]
    },
    "guides": {
        "label": "Гайды и промпты",
        "emoji": "📚",
        "items": [
            [
                "Prompting AI for Open-Source Intelligence",
                "https://www.osintcombine.com/post/prompting-ai"
            ],
            [
                "Prompt Engineering For OSINT",
                "https://osintteam.blog/prompt-engineering-for-osint-0301d323cb6d"
            ],
            [
                "A new gen of OSINT, AI",
                "https://osintteam.blog/artificial-intelligence-in-the-service-of-osint-a-revolution-in-information-analysis-4870e0e02163"
            ],
            [
                "AI and OSINT: Crafting Superior Investigation Strategies",
                "https://medium.com/@efim.lerner/ai-and-osint-crafting-superior-investigation-strategies-addccb3012db"
            ],
            [
                "How to enhance OSINT investigations using AI",
                "https://www.techtarget.com/searchenterpriseai/tip/How-to-enhance-OSINT-investigations-using-AI"
            ],
            [
                "The Impact of AI with OSINT",
                "https://www.youtube.com/watch?v=zgIteU4jEZs"
            ],
            [
                "Generative AI for OSINT: Next Level Techniques for ChatGPT and Beyond",
                "https://www.youtube.com/watch?v=7zzubjKEUW4"
            ],
            [
                "How to use ChatGPT for OSINT",
                "https://www.reddit.com/r/OSINT/comments/10tq6iz/how_to_use_chatgpt_for_osint/"
            ],
            [
                "103 OSINT ChatGPT prompt ideas",
                "https://noticias.ai/wp-content/uploads/2023/03/103-osint-chatgpt-prompt-ideas.pdf"
            ],
            [
                "OSINT Workflow with ChatGPT: Tips, Risks, and Benefits",
                "https://www.osintcombine.com/post/osint-workflow-with-chatgpt-tips-risks-and-benefits"
            ],
            [
                "ChatGPT Prompting Cheat Sheet for OSINT",
                "https://knowlesys.com/osint/chatgpt_prompting_cheat_sheet.html"
            ],
            [
                "Utilizing ChatGPT for OSINT Investigations",
                "https://osintteam.blog/utilizing-chatgpt-for-osint-investigations-8299ce16dd28"
            ],
            [
                "Intel Sourcing Agent",
                "https://chatgpt.com/g/g-HcFHDwAdM-intel-sourcing-agent"
            ],
            [
                "OSINT360 Cyber Intelligence GPT",
                "https://github.com/oryon-osint/OSINT360-GPT"
            ],
            [
                "GROKINT – Using Grok AI for X OSINT",
                "https://joellatto.com/2025/08/13/grokint-using-grok-ai-for-x-osint/"
            ],
            [
                "How AI is Changing OSINT: Investigating X Accounts with Grok",
                "https://www.netwatchglobal.com/blogs/how-ai-is-changing-osint-investigating-x-accounts-with-grok/"
            ],
            [
                "Investigating X Profiles and Posts Using Grok AI",
                "https://bank-security.medium.com/investigating-x-profiles-and-posts-using-grok-ai-91157776783e"
            ],
            [
                "Claude AI for OSINT: What It Does & Where It Fits",
                "https://www.linkedin.com/pulse/claude-ai-osint-what-does-where-fits-toddington-international-yd6yc"
            ]
        ]
    }
}

CATEGORY_ORDER = ["face", "username", "contacts", "geoint", "image", "dorks", "net", "assistants", "events", "detect", "skills", "guides"]
