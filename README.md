# Job Agent MVP

本项目是一个“课程大纲与岗位池匹配智能体”原型系统，用于把课程大纲中的知识点、能力要求与招聘岗位要求进行结构化匹配，并通过 Web 工作台展示课程画像、岗位池、知识图谱状态和智能匹配结果。

## 项目功能

- 教学大纲入库：支持 PDF、扫描版 PDF、DOCX、TXT、MD。
- 课程画像分析：抽取课程知识点、能力、技术、工具、方法和岗位方向。
- 岗位池管理：支持招聘岗位抓取、岗位详情保存、SQLite 存储和 JSON 导出。
- 知识图谱：将课程能力和岗位要求结构化为图节点，并同步到 Neo4j。
- GraphRAG 匹配：基于 Neo4j fulltext index 和 vector index 进行混合召回，生成课程与岗位的匹配解释。
- Web 工作台：支持上传大纲、查看岗位、构建知识图谱、预览图谱证据和运行智能匹配。

## 技术栈

- 后端：Python、FastAPI、Uvicorn
- 前端：HTML、CSS、JavaScript
- 数据存储：SQLite
- 知识图谱：Neo4j
- 浏览器自动化：Playwright
- 大模型接口：OpenAI-compatible API

## 项目结构

```text
job_agent/                     后端核心代码
job_agent/sources/             招聘平台数据源适配
web/                           Web 工作台前端
scripts/                       辅助脚本
docker-compose.neo4j.yml       本地 Neo4j 启动配置
requirements.txt               Python 依赖
.env.example                   环境变量示例
```

## 本地安装

```powershell
cd job_agent_mvp
$env:PYTHONUTF8="1"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
python -m job_agent.app init-db
```

## 环境变量

复制 `.env.example` 为 `.env`，然后填写自己的接口配置：

```env
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
OPENAI_EMBEDDING_MODEL=text-embedding-3-small

NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_neo4j_password
NEO4J_DATABASE=neo4j
```

`.env` 包含本地密钥和密码，只用于本地运行，不应该提交到 GitHub。

## 启动 Neo4j

本项目的知识图谱和匹配功能依赖 Neo4j。匹配时不会临时建图，必须先构建并同步课程图谱和岗位图谱。

```powershell
docker compose -f docker-compose.neo4j.yml up -d
```

Neo4j 浏览器：

```text
http://localhost:7474
```

默认本地连接信息：

```text
用户名：neo4j
密码：你的 Neo4j 密码
数据库：neo4j
Bolt 地址：bolt://localhost:7687
```

如果使用 `docker-compose.neo4j.yml` 启动本地 Neo4j，请确保 `.env` 中的 `NEO4J_PASSWORD` 与 compose 文件里的 `NEO4J_AUTH` 密码保持一致。

## 启动 Web 工作台

```powershell
cd job_agent_mvp
$env:PYTHONUTF8="1"
.\.venv\Scripts\python.exe -m uvicorn job_agent.web_app:app --host 127.0.0.1 --port 8765
```

浏览器打开：

```text
http://127.0.0.1:8765/
```

## 使用流程

1. 配置 `.env`。
2. 初始化数据库。
3. 启动 Neo4j。
4. 启动 Web 工作台。
5. 上传课程大纲并生成课程画像。
6. 抓取或导入岗位数据。
7. 在“知识图谱”模块构建课程图谱和岗位图谱。
8. 在“智能匹配”模块选择课程并生成岗位匹配解释。

## 命令行示例

保存招聘平台登录态：

```powershell
python -m job_agent.app auth login 51job
python -m job_agent.app auth status
```

抓取岗位：

```powershell
python -m job_agent.app run "查询计算机专业在广州的对口岗位，抓取1页" --source 51job --no-headless
```

查看岗位：

```powershell
python -m job_agent.app list --limit 20
python -m job_agent.app show 60
python -m job_agent.app export-json --output .\data\jobs.json
```

添加课程大纲：

```powershell
python -m job_agent.app syllabus add "path\to\软件工程教学大纲.pdf" --analyze
python -m job_agent.app syllabus list
python -m job_agent.app syllabus profile 6
```

运行匹配：

```powershell
python -m job_agent.app match --syllabus 5,6 --limit 5 --candidate-limit 60
python -m job_agent.app match-run list
python -m job_agent.app match-run show 2
```

## 匹配算法说明

系统先把课程大纲和岗位 JD 抽取成结构化节点：

- `Syllabus`：课程大纲
- `CourseNode`：课程知识点、能力、工具、方法、岗位方向
- `Job`：岗位
- `Company`：公司
- `JobRequirement`：岗位职责、技能、工具、经验、学历、领域要求

核心关系：

- `Syllabus -[:HAS_COURSE_NODE]-> CourseNode`
- `Company -[:POSTED]-> Job`
- `Job -[:HAS_REQUIREMENT]-> JobRequirement`
- `CourseNode -[:SUPPORTS|PARTIALLY_SUPPORTS|RELATED_TO]-> JobRequirement`

匹配时使用实体级 GraphRAG 和混合检索：

```text
选中课程
  -> 从 Neo4j 读取课程能力节点
  -> 使用 fulltext index 检索字面相关岗位要求
  -> 使用 vector index 检索语义相关岗位要求
  -> 合并候选证据包
  -> 由模型基于证据生成匹配解释
  -> 将确认后的关系写回 Neo4j
```

