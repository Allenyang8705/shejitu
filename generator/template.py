"""日报模板管理（design.md §2.2）。

内置 Markdown 与 HTML 两套 Jinja2 模板，渲染同一份 formatter 视图数据，
内容天然一致——不引入 Markdown→HTML 转换库（邮件 HTML 需要内联样式，
且数据同源无漂移风险）。

本期不做外部自定义加载：design.md 未定义自定义来源，tasks.md 验收不要求；
需要时在此追加模板读取逻辑即可，渲染入口不变。
"""

from jinja2 import Environment, Template

_MARKDOWN_ENV = Environment(trim_blocks=True, lstrip_blocks=True, autoescape=False)
# HTML 模板必须转义：消息内容来自群聊原文，直接进邮件（design.md §6.2 安全）
_HTML_ENV = Environment(trim_blocks=True, lstrip_blocks=True, autoescape=True)

_MARKDOWN_SOURCE = """\
# {{ team_name }} 日报（{{ date }}）
{% for member in members %}
## {{ member.name }}
{% if not member.has_any %}
今日无记录
{% else %}
### 代码提交
{% if member.commits_failed %}
> ⚠ {{ member.commits_failed_label }}
{% endif %}
{% for commit in member.commits %}
- `{{ commit.repo }}` {{ commit.time }}（+{{ commit.additions }}/-{{ commit.deletions }}）{{ commit.message }}
{% endfor %}
{% if not member.commits and not member.commits_failed %}
今日无记录
{% endif %}

### 协作沟通
{% if member.messages_failed %}
> ⚠ {{ member.messages_failed_label }}
{% endif %}
{% for message in member.messages %}
- {{ message.time }} {{ message.sender }}（{{ message.chat }}）：{{ message.content }}
{% endfor %}
{% if not member.messages and not member.messages_failed %}
今日无记录
{% endif %}
{% endif %}

{% endfor %}"""

_HTML_SOURCE = """\
<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>{{ team_name }} 日报（{{ date }}）</title>
    <style>
      body { font-family: "Microsoft YaHei", "PingFang SC", sans-serif; max-width: 800px; margin: 0 auto; color: #222; }
      h3 { border-bottom: 1px solid #ddd; padding-bottom: 4px; margin-bottom: 8px; }
      ul { margin-top: 0; }
      .failed { color: #b45309; }
      .empty { color: #888; }
      footer { margin-top: 32px; color: #888; font-size: 12px; }
    </style>
  </head>
  <body>
    <h1>{{ team_name }} 日报（{{ date }}）</h1>
    {% for member in members %}
    <section>
      <h2>{{ member.name }}</h2>
      {% if not member.has_any %}
      <p class="empty">今日无记录</p>
      {% else %}
      <h3>代码提交</h3>
      {% if member.commits_failed %}<p class="failed">⚠ {{ member.commits_failed_label }}</p>{% endif %}
      {% if member.commits %}
      <ul>
        {% for commit in member.commits %}
        <li><code>{{ commit.repo }}</code> {{ commit.time }}（+{{ commit.additions }}/-{{ commit.deletions }}）{{ commit.message }}</li>
        {% endfor %}
      </ul>
      {% else %}<p class="empty">今日无记录</p>{% endif %}
      <h3>协作沟通</h3>
      {% if member.messages_failed %}<p class="failed">⚠ {{ member.messages_failed_label }}</p>{% endif %}
      {% if member.messages %}
      <ul>
        {% for message in member.messages %}
        <li>{{ message.time }} {{ message.sender }}（{{ message.chat }}）：{{ message.content }}</li>
        {% endfor %}
      </ul>
      {% else %}<p class="empty">今日无记录</p>{% endif %}
      {% endif %}
    </section>
    {% endfor %}
    <footer>生成于 {{ generated_at }}（UTC+8）</footer>
  </body>
</html>"""

MARKDOWN_TEMPLATE = _MARKDOWN_ENV.from_string(_MARKDOWN_SOURCE)
HTML_TEMPLATE = _HTML_ENV.from_string(_HTML_SOURCE)


def get_markdown_template() -> Template:
    """获取 Markdown 日报模板。"""
    return MARKDOWN_TEMPLATE


def get_html_template() -> Template:
    """获取 HTML 日报模板。"""
    return HTML_TEMPLATE
