from decimal import Decimal
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from psycopg import sql
import psycopg
import sync_bot as bot


SPEC = {"schema": "public", "table": "produtos", "key": ["codigo"],
        "columns": ["descricao", "preco"]}


def row(code, price, description="Produto"):
    return {"codigo": code, "descricao": description, "preco": price}


class ComparisonTests(unittest.TestCase):
    def test_updates_only_selected_changed_columns(self):
        report, updates = bot.compare(SPEC, [row("A", Decimal("12.00"))], [row("A", Decimal("10"))])
        self.assertEqual(report["updates"][0]["before"], {"preco": {"decimal": "10"}})
        self.assertEqual(updates[0][1], ["preco"])

    def test_no_inserts_or_deletes(self):
        report, updates = bot.compare(SPEC, [row("LOCAL", 1)], [row("PROD", 2)])
        self.assertEqual(updates, [])
        self.assertEqual(report["local_only_skipped"], 1)
        self.assertEqual(report["production_only_untouched"], 1)
        self.assertEqual(report["production_only_rows"], [
            {"key": {"codigo": "PROD"}, "values": row("PROD", 2)}])

    def test_production_only_rows_are_separate_from_updates_and_stably_sorted(self):
        source = [row("A", 5)]
        target = [row("C", 9), row("A", 1), row("B", 8)]
        report, updates = bot.compare(SPEC, source, target)
        self.assertEqual([r["key"]["codigo"] for r in report["production_only_rows"]], ["B", "C"])
        self.assertEqual([u[0] for u in updates], [("A",)])
        self.assertEqual(target[1]["preco"], 1)

    def test_empty_local_values_preserve_production(self):
        for value in (None, "", "   ", "\t\n", [], {}, ()):
            with self.subTest(value=value):
                report, updates = bot.compare(SPEC, [row("A", value)], [row("A", 10)])
                self.assertEqual(report["updates"], [])
                self.assertEqual(updates, [])

    def test_false_and_zero_are_valid_updates(self):
        for value, previous in ((False, True), (0, 10), (Decimal("0"), Decimal("10"))):
            with self.subTest(value=value):
                report, updates = bot.compare(SPEC, [row("A", value)], [row("A", previous)])
                self.assertEqual(len(updates), 1)
                self.assertEqual(report["updates"][0]["after"], {"preco": bot.normal(value)})

    def test_filled_local_value_can_replace_production_null(self):
        report, updates = bot.compare(SPEC, [row("A", 10)], [row("A", None)])
        self.assertEqual(report["updates"][0]["after"], {"preco": 10})
        self.assertEqual(len(updates), 1)

    def test_product_protection_changes_but_empty_links_are_ignored(self):
        spec = dict(SPEC, columns=["protecao", "datasheet", "url", "data"])
        production = {"codigo": "4301771", "protecao": "IP67 / NEMA 4X",
                      "datasheet": "https://example.com/datasheet.pdf",
                      "url": "https://example.com/produto", "data": "2026-09-13"}
        local = dict(production, protecao="IP67 / NEMA 4X / C5-H",
                     datasheet="", url=None, data=None)
        report, updates = bot.compare(spec, [local], [production])
        self.assertEqual(report["updates"][0]["after"], {"protecao": "IP67 / NEMA 4X / C5-H"})
        self.assertEqual(updates[0][1], ["protecao"])

    def test_equal_decimal_scale_does_not_update(self):
        _, updates = bot.compare(SPEC, [row("A", Decimal("10.00"))], [row("A", Decimal("10"))])
        self.assertEqual(updates, [])

    def test_nan_equality(self):
        self.assertTrue(bot.same(float("nan"), float("nan")))
        self.assertTrue(bot.same(Decimal("NaN"), Decimal("NaN")))

    def test_duplicate_and_null_keys_refused_in_both_banks(self):
        for invalid in ([row("A", 1), row("A", 2)], [row(None, 1)]):
            for source, target in ((invalid, []), ([], invalid)):
                with self.assertRaises(ValueError):
                    bot.compare(SPEC, source, target)

    def test_composite_key(self):
        spec = dict(SPEC, key=["codigo", "loja"])
        a = dict(row("A", 5), loja=1)
        b = dict(row("A", 6), loja=2)
        report, _ = bot.compare(spec, [a, b], [dict(a, preco=4), b])
        self.assertEqual(report["updates"][0]["key"], {"codigo": "A", "loja": 1})

    def test_hash_is_stable_and_detects_edited_plan(self):
        a, _ = bot.compare(SPEC, [row("A", 5), row("B", 6)], [row("A", 1), row("B", 2)])
        b, _ = bot.compare(SPEC, [row("B", 6), row("A", 5)], [row("B", 2), row("A", 1)])
        self.assertEqual(bot.digest(a), bot.digest(b))
        b["updates"][0]["after"]["preco"] = 99
        self.assertNotEqual(bot.digest(a), bot.digest(b))

    def test_invalid_configuration(self):
        for config in ({"tables": []}, {"tables": [SPEC, SPEC]},
                       {"tables": [dict(SPEC, columns=["codigo"])]}):
            with self.assertRaises(ValueError):
                bot.validate(config)

    def test_multiple_tables(self):
        bot.validate({"tables": [SPEC, dict(SPEC, table="precos")]})

    def test_wildcard_validation_and_resolution(self):
        spec = dict(SPEC, columns="*")
        bot.validate({"tables": [spec]})
        meta = {name: {"is_generated": "NEVER", "is_identity": "NO"}
                for name in ("codigo", "descricao", "preco", "id", "calculado")}
        meta["id"]["is_identity"] = "YES"
        meta["calculado"]["is_generated"] = "ALWAYS"
        resolved = bot.resolve_columns(spec, meta, meta)
        self.assertEqual(resolved["columns"], ["descricao", "preco"])
        self.assertEqual(spec["columns"], "*")

    def test_wildcard_refuses_schema_difference(self):
        spec = dict(SPEC, columns="*")
        with self.assertRaises(ValueError):
            bot.resolve_columns(spec, {"codigo": {}}, {"codigo": {}, "extra": {}})

    def test_wildcard_respects_generation_in_either_database(self):
        source = {"codigo": {"is_generated": "NEVER", "is_identity": "NO"},
                  "valor": {"is_generated": "ALWAYS", "is_identity": "NO"}}
        target = {"codigo": source["codigo"],
                  "valor": {"is_generated": "NEVER", "is_identity": "NO"}}
        with self.assertRaises(ValueError):
            bot.resolve_columns(dict(SPEC, columns="*"), source, target)

    def test_report_binds_discovered_columns_even_without_changes(self):
        a, _ = bot.compare(SPEC, [], [])
        b, _ = bot.compare(dict(SPEC, columns=["descricao"]), [], [])
        self.assertNotEqual(bot.digest(a), bot.digest(b))


class ApplyTests(unittest.TestCase):
    def work(self):
        return [(SPEC, [(('A',), ['preco'], row('A', 5))],
                 {"preco": {"udt_name": "numeric"}},
                 {('A',): row('A', 5), ('B',): row('B', 9)})]

    def test_update_is_parameterized_and_checks_final_rows(self):
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1
        with patch.object(bot, "read_rows", return_value=[row('A', 5), row('B', 9)]):
            self.assertEqual(bot.execute_updates(conn, self.work(), sql), 1)
        query, values = conn.execute.call_args.args
        self.assertEqual(query.as_string(), 'UPDATE "public"."produtos" SET "preco" = %s WHERE "codigo" = %s')
        self.assertEqual(values, [5, 'A'])

    def test_missing_row_or_duplicate_match_refuses_update(self):
        for count in (0, 2):
            conn = MagicMock()
            conn.execute.return_value.rowcount = count
            with self.assertRaises(ValueError):
                bot.execute_updates(conn, self.work(), sql)

    def test_trigger_effect_on_untouched_row_refused(self):
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1
        with patch.object(bot, "read_rows", return_value=[row('A', 5), row('B', 999)]):
            with self.assertRaises(ValueError):
                bot.execute_updates(conn, self.work(), sql)

    def test_trigger_insert_refused(self):
        conn = MagicMock()
        conn.execute.return_value.rowcount = 1
        with patch.object(bot, "read_rows", return_value=[row('A', 5), row('B', 9), row('C', 1)]):
            with self.assertRaises(ValueError):
                bot.execute_updates(conn, self.work(), sql)


class TextReportTests(unittest.TestCase):
    def test_utf8_report_contains_preserved_rows_and_tables_without_extras(self):
        first, _ = bot.compare(SPEC, [], [row("P", Decimal("5.00"), "Câmera")])
        second, _ = bot.compare(dict(SPEC, table="precos"), [], [])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "preservados.txt"
            self.assertEqual(bot.save_untouched_report({"tables": [first, second]}, 3, path), 1)
            text = path.read_text(encoding="utf-8")
            self.assertIn("Câmera", text)
            self.assertIn('"codigo": "P"', text)
            self.assertIn("public.precos", text)
            self.assertIn("Nenhum registro", text)
            self.assertIn("Registros atualizados em produção: 3", text)
            with self.assertRaises(FileExistsError):
                bot.save_untouched_report({"tables": [first]}, 3, path)
            self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_txt_is_written_only_after_successful_transaction_exit(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temp:
                config = {"tables": [SPEC]}
                table, _ = bot.compare(SPEC, [], [row("P", 5)])
                plan = {"version": 4, "config": config, "tables": [table]}
                config_path, report_path = Path(temp) / "config.json", Path(temp) / "relatorio.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                report_path.write_text(json.dumps(plan), encoding="utf-8")
                source, target = MagicMock(), MagicMock()
                source.__enter__.return_value = source
                target.__enter__.return_value = target
                target_tx = target.transaction.return_value

                def verify_commit_then_save(*args):
                    self.assertTrue(target_tx.__exit__.called)
                    self.assertIsNone(target_tx.__exit__.call_args.args[0])
                    return 1

                argv = ["sync_bot.py", "apply", "--config", str(config_path),
                        "--report", str(report_path), "--approve", bot.digest(plan)]
                with patch("sys.argv", argv), patch.dict("os.environ", {"LOCAL_DSN": "local", "PRODUCTION_DSN": "prod"}), \
                     patch.object(bot, "connect_database", side_effect=[source, target]), \
                     patch.object(bot, "build_plan", return_value=(plan, [])), \
                     patch.object(bot, "execute_updates", side_effect=ValueError("falha") if fail else None, return_value=2), \
                     patch.object(bot, "save_untouched_report", side_effect=verify_commit_then_save) as save, \
                     contextlib.redirect_stdout(io.StringIO()):
                    if fail:
                        with self.assertRaises(ValueError):
                            bot.main()
                        save.assert_not_called()
                    else:
                        bot.main()
                        save.assert_called_once()


class ConnectionTests(unittest.TestCase):
    def test_details_redact_uri_password_and_environment_password(self):
        dsn = "postgresql://usuario:s%40nh%40@localhost/banco"
        exc = psycopg.OperationalError("falha localizada s@nh@ s%40nh%40 ambiente " + dsn)
        with patch.dict("os.environ", {"PGPASSWORD": "ambiente"}):
            detail = bot.safe_connection_detail(exc, dsn)
        self.assertIn("falha localizada", detail)
        for secret in ("s@nh@", "s%40nh%40", "ambiente", dsn):
            self.assertNotIn(secret, detail)

    def test_portuguese_authentication_error(self):
        exc = psycopg.OperationalError("FATAL: autenticação com senha falhou para usuário")
        self.assertIn("autenticação", bot.connection_error_reason(exc))

    def test_connection_diagnostics_do_not_leak_secrets(self):
        cases = [
            ("password authentication failed password=segredo", "autenticação"),
            ("could not translate host name segredo", "Servidor não encontrado"),
            ("connection refused segredo", "Conexão recusada"),
            ("timeout expired segredo", "Tempo de conexão"),
            ("no pg_hba.conf entry segredo", "não autorizou"),
            ("SSL certificate verify failed segredo", "SSL/certificado"),
            ('database "segredo" does not exist', "não encontrado"),
            ("unexpected internal error segredo", "Não foi possível conectar"),
        ]
        for detail, expected in cases:
            with self.subTest(detail=detail):
                with patch("psycopg.connect", side_effect=psycopg.OperationalError(detail)):
                    with self.assertRaises(ValueError) as error:
                        bot.connect_database("password=segredo", "produção")
                self.assertIn("banco produção", str(error.exception))
                self.assertIn(expected, str(error.exception))
                self.assertNotIn("segredo", str(error.exception))


if __name__ == '__main__':
    unittest.main()
