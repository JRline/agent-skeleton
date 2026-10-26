"""The conversation as one HTML page: requests with files, folded steps, answers with tables, sources, timing."""
from agent.backends.fake import DemoFakeModel
from agent.export import _md, build_html, export_html
from agent.loop import run

from tests.helpers import TempDirs


class ExportHtml(TempDirs):
    def test_whole_conversation_in_one_page(self):
        state = run("Compute the latest capital ratio from inputs/sample_capital.csv", DemoFakeModel(),
                    inputs=["inputs/sample_capital.csv"], approve=lambda e: True)
        page = build_html(state)
        self.assertIn("<div class='label'>Your request</div>", page)
        self.assertIn("📎 sample_capital.csv", page)
        self.assertIn("<summary>How I got here", page)
        self.assertIn("<sup><a href='#s1-1'>[1]</a></sup>", page)               # citations link to sources
        self.assertIn("id='s1-1'", page)
        self.assertIn("<th>cet1_ratio_pct</th>", page)                           # the result table, as a table
        self.assertIn("Answered in", page)
        self.assertNotIn("<script", page)                                        # opens offline, nothing runs
        self.assertTrue(export_html(state).read_text().startswith("<!doctype html>"))

    def test_markdown_is_converted_and_escaped(self):
        html = _md("## Head\n**bold** and `code` <b>raw</b>\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- one\n- two\n\n"
                   "```sql\nSELECT 1\n```")
        self.assertIn("<h3>Head</h3>", html)
        self.assertIn("<b>bold</b> and <code>code</code> &lt;b&gt;raw&lt;/b&gt;", html)
        self.assertIn("<th>a</th>", html)
        self.assertIn("<ul><li>one</li><li>two</li></ul>", html)
        self.assertIn("<pre><code>SELECT 1</code></pre>", html)
