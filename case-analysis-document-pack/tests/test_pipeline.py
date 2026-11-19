#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端样例：不连法院、不提交，只验证分析→确认→渲染链路。"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SKILLS = Path(r"d:\Project\skills")
NEW = SKILLS / "case-analysis-document-pack"
OLD = SKILLS / "court-civil-first-instance-filing"
PY = sys.executable


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cadp-test-"))
        self.root = self.tmp / "workspace"
        self.root.mkdir()
        self.run_cmd([PY, OLD / "scripts/bootstrap_case.py", "--root", self.root, "--fyid", "0000",
                  "--court", "待确认", "--region", "", "--cause", "买卖合同纠纷",
                  "--plaintiff", "甲公司", "--defendant", "乙公司", "--case-type", "sp"])
        self.case_dir = next((self.root / "案件库").iterdir())

    def tearDown(self) -> None:
        for path in self.tmp.rglob("*"):
            try:
                path.chmod(0o666)
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, cmd, expect: int | None = None):
        result = subprocess.run([str(x) for x in cmd], capture_output=True, text=True)
        if expect is not None and result.returncode != expect:
            self.fail(f"exit {result.returncode} != {expect}\nstdout={result.stdout}\nstderr={result.stderr}")
        return result

    def _write(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_pipeline(self) -> None:
        # 1. 聊天导出（中性买卖合同样例）
        self._write(self.case_dir / "01原始包" / "聊天导出.txt", "\n".join([
            "2024-03-01 09:00 甲: 合同价款100000元，约定3月10日交货",
            "2024-03-02 10:00 乙: 收到，按期交货",
            "2024-03-11 09:00 甲: 货未到，请尽快交付",
        ]))
        sys.path.insert(0, str(NEW / "scripts"))
        from docx_helper import write_docx
        write_docx(self.case_dir / "04未签稿" / "聊天导出.docx", "聊天导出", [
            "2024-03-12 10:00 乙: 已安排发货"
        ])

        # 2. 解析
        self.run_cmd([PY, NEW / "scripts/parse_chat_records.py", "--case-dir", self.case_dir])
        chat = json.loads((self.case_dir / "05案件信息/chat_evidence.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(chat["stats"]["message_count"], 3)
        self.assertTrue(any(m.get("amount") == "100000" for m in chat["messages"]))

        # 3. 离线管辖
        self.run_cmd([PY, NEW / "scripts/suggest_court_offline.py", "--case-dir", self.case_dir, "--json"])

        # 4. 双源探测
        tools = {
            "yuandian.law": "mcp__yuandian-mcp__yuandian_semantic_search_legal_articles",
            "yuandian.case": "mcp__yuandian-mcp__yuandian_semantic_search_cases",
            "qcc.law": "mcp__qcc-legal-regulation__get_legal_article_semantic_search",
            "qcc.case": "mcp__qcc-legal-case__get_judicial_case_search",
        }
        self._write(self.case_dir / "06运行记录" / "resolved_tools.json", tools)
        self.run_cmd([PY, NEW / "scripts/yuandian_search.py", "probe", "--case-dir", self.case_dir,
                  "--tools-json", self.case_dir / "06运行记录/resolved_tools.json",
                  "--providers", "yuandian,qcc"])

        # 5. mock 双源返回并 ingest
        self._write(self.case_dir / "06运行记录" / "yuandian_law.json", {
            "code": 200, "message": "ok", "data": {"list": [
                {"title": "中华人民共和国民法典", "article": "第五百七十七条",
                 "content": "当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担继续履行、采取补救措施或者赔偿损失等违约责任。",
                 "url": "https://example.test/yuandian-law"}]}})
        self._write(self.case_dir / "06运行记录" / "qcc_law.json", {
            "code": 200, "message": "ok", "data": {"list": [
                {"regulationName": "中华人民共和国民法典", "articleNo": "第五百七十七条",
                 "articleContent": "当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担继续履行、采取补救措施或者赔偿损失等违约责任。",
                 "effectStatus": "现行有效", "url": "https://example.test/qcc-law"}]}})
        self._write(self.case_dir / "06运行记录" / "yuandian_case.json", {
            "code": 200, "message": "ok", "data": {"list": [
                {"case_number": "（2023）浙0100民初1号", "court": "浙江省杭州市某区人民法院",
                 "decision_date": "2023-05-01", "title": "买卖合同纠纷", "content": "支持继续履行。"}]}})
        self._write(self.case_dir / "06运行记录" / "qcc_case.json", {
            "code": 200, "message": "ok", "data": {"list": [
                {"caseNo": "（2023）浙0100民初1号", "courtName": "浙江省杭州市某区人民法院",
                 "judgmentDate": "2023-05-01", "title": "买卖合同纠纷", "content": "支持继续履行。"}]}})
        for provider, kind, filename in (
            ("yuandian", "law", "yuandian_law.json"),
            ("qcc", "law", "qcc_law.json"),
            ("yuandian", "case", "yuandian_case.json"),
            ("qcc", "case", "qcc_case.json"),
        ):
            self.run_cmd([PY, NEW / "scripts/yuandian_search.py", "ingest", "--provider", provider,
                      "--kind", kind, "--raw", self.case_dir / "06运行记录" / filename,
                      "--case-dir", self.case_dir, "--province", "浙江省"])

        state = json.loads((self.case_dir / "05案件信息/analysis.json").read_text(encoding="utf-8"))
        self.assertTrue(state["law_search"]["cross_check"]["both_returned"])
        self.assertTrue(state["case_search"]["cross_check"]["both_returned"])

        # 6. 分析报告
        findings = {
            "case_summary": {
                "case_type": "sp", "cause": "买卖合同纠纷", "plaintiff": "甲公司",
                "defendant": "乙公司", "amount": "100000", "court": "待确认",
                "claims": "请求判令乙公司继续履行交货义务并承担违约责任",
                "plaintiff_id": "91330000TEST000001", "plaintiff_address": "某市某区某路1号",
                "plaintiff_phone": "13800000000", "defendant_id": "91330000TEST000002",
                "defendant_address": "某市某区某路2号",
            },
            "legal_relations": {
                "step1": "争议焦点为乙公司是否迟延交货",
                "step2": "已产生买卖合同法律关系",
                "step3": "买卖合同关系",
                "step4": "主体：甲公司、乙公司；内容：交货与付款；客体：货物及价款",
                "step5": "2024-03-01 成立，2024-03-10 到期未交付",
                "syllogism": "小前提：合同约定3月10日交货；大前提：民法典第五百七十七条；结论：乙公司可能承担违约责任。",
            },
            "facts": [
                {"text": "合同价款100000元，约定3月10日交货", "date": "2024-03-01",
                 "source": {"file": "01原始包/聊天导出.txt", "locator": "L1", "quote": "约定3月10日交货"},
                 "confidence": "high"},
                {"text": "乙公司未按期交货", "date": "2024-03-11",
                 "source": {"file": "01原始包/聊天导出.txt", "locator": "L3", "quote": "货未到"},
                 "confidence": "high"},
            ],
            "dispute_focus": [{
                "focus": "乙公司是否迟延交货及违约责任", "plaintiff_view": "应继续履行并赔偿",
                "defendant_view": "可能主张已安排发货", "analysis": "需核对交付凭证",
                "fact_ids": ["F-0001"], "law_source_ids": ["LAW-test"],
            }],
            "evidence_grid": [{
                "claim": "买卖合同成立及交货期限", "fact_ids": ["F-0001"],
                "evidence": "聊天导出文件", "weakness": "需核对聊天主体身份",
            }],
            "reference_conclusion": {
                "conclusion": "在合同成立且未按期交付的前提下，甲公司主张继续履行具有基础。",
                "favorable": "聊天记录能证明合同与交货期限。",
                "most_likely": "支持继续履行或赔偿，具体取决于交付事实。",
                "unfavorable": "若乙公司已交付或甲公司未付款，结论可能变化。",
                "strategy": ["补交货凭证", "核对付款节点"],
                "evidence_todo": ["物流单", "签收记录"],
            },
            "risks": ["聊天主体身份需确认"],
            "questions": ["确认聊天双方身份", "确认是否已交付"],
        }
        self._write(self.case_dir / "06运行记录" / "agent_findings.json", findings)
        self.run_cmd([PY, NEW / "scripts/analyze_case.py", "--case-dir", self.case_dir,
                  "--findings", self.case_dir / "06运行记录/agent_findings.json"])
        report = (self.case_dir / "案件分析报告.md").read_text(encoding="utf-8")
        self.assertIn("参考结论与策略建议", report)
        self.assertIn("案件补全清单", report)
        self.assertIn("双源核对", report)

        # 7. 确认前渲染必须失败
        self.run_cmd([PY, NEW / "scripts/render_documents.py", "--case-dir", self.case_dir], expect=1)
        self.run_cmd([PY, NEW / "scripts/confirm_gate.py", "--case-dir", self.case_dir, "--confirm"])
        self.run_cmd([PY, NEW / "scripts/confirm_gate.py", "--case-dir", self.case_dir,
                  "--confirm", "--complaint-format", "ordinary"])
        self.run_cmd([PY, NEW / "scripts/confirm_gate.py", "--case-dir", self.case_dir, "--check"])

        # 8. 渲染整套 Word（模板必须来自实战包，不允许骨架）
        self.run_cmd([PY, NEW / "scripts/manage_templates.py", "--root", self.root, "--case-type", "sp"])
        self.run_cmd([PY, NEW / "scripts/render_documents.py", "--case-dir", self.case_dir])
        manifest = json.loads((self.case_dir / "05案件信息/document_manifest.json").read_text(encoding="utf-8"))
        for doc in manifest["documents"]:
            self.assertTrue(doc["template_status"].startswith("local_practice"), doc)
            self.assertTrue(doc["template_key"], doc)

        # 8.1 模板只读：副本不可写、与源一致
        tpl_dir = self.root / "通用件" / "模板"
        for tpl in tpl_dir.iterdir():
            if tpl.name == "来源登记.json":
                continue
            self.assertFalse(tpl.stat().st_mode & 0o200, f"模板应只读：{tpl}")
            origin = NEW / "assets" / "templates" / tpl.name
            if origin.exists():
                self.assertEqual(tpl.read_bytes(), origin.read_bytes(), tpl)
        self.run_cmd([PY, NEW / "scripts/manage_templates.py", "--root", self.root, "--verify"], expect=0)

        # 8.2 手改副本 → verify 必须报错，restore 能还原
        victim = next(p for p in tpl_dir.iterdir() if p.suffix == ".docx")
        victim.chmod(0o666)
        victim.write_bytes(b"tampered")
        self.run_cmd([PY, NEW / "scripts/manage_templates.py", "--root", self.root, "--verify"], expect=1)
        self.run_cmd([PY, NEW / "scripts/manage_templates.py", "--root", self.root, "--restore"], expect=0)
        self.run_cmd([PY, NEW / "scripts/manage_templates.py", "--root", self.root, "--verify"], expect=0)
        self.assertFalse(victim.stat().st_mode & 0o200, victim)
        docs = [p for p in (self.case_dir / "04未签稿").glob("*.docx") if p.name[:2].isdigit()]
        self.assertGreaterEqual(len(docs), 4)
        for doc in docs:
            self.assertTrue(zipfile.is_zipfile(doc), doc)
            with zipfile.ZipFile(doc) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            self.assertTrue("签字" in xml or "盖章" in xml, doc)

        # 9. 客户未签字，preflight 应拒绝
        self.run_cmd([PY, OLD / "scripts/preflight_materials.py", "--case-dir", self.case_dir,
                  "--case-type", "sp"], expect=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
