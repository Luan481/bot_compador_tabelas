"""Compare PostgreSQL tables and apply an explicitly reviewed update plan."""
import argparse
import base64
import datetime as dt
import decimal
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sys
import uuid
from urllib.parse import quote, quote_plus


def normal(value):
    """Stable, typed representation for reports; never used as SQL input."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return {"float": repr(value)}
    if isinstance(value, decimal.Decimal):
        return {"decimal": str(value)}
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return {type(value).__name__: value.isoformat()}
    if isinstance(value, dt.timedelta):
        return {"timedelta": str(value)}
    if isinstance(value, uuid.UUID):
        return {"uuid": str(value)}
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {k: normal(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [normal(v) for v in value]
    raise ValueError(f"Tipo de valor ainda não suportado: {type(value).__name__}")


def same(a, b):
    # PostgreSQL considers NaN equal to NaN.
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    if isinstance(a, decimal.Decimal) and isinstance(b, decimal.Decimal) and a.is_nan() and b.is_nan():
        return True
    return a == b


def empty_source(value):
    """Absent local values must never erase existing production data."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return False


def validate(config):
    if set(config) != {"tables"} or not config["tables"]:
        raise ValueError("Configure uma lista não vazia em tables.")
    seen = set()
    for spec in config["tables"]:
        if set(spec) != {"schema", "table", "key", "columns"}:
            raise ValueError("Cada tabela deve conter schema, table, key e columns.")
        for field in ("schema", "table"):
            if not isinstance(spec[field], str) or not spec[field]:
                raise ValueError(f"Nome inválido: {field}")
        for field in ("key", "columns"):
            values = spec[field]
            if field == "columns" and values == "*":
                continue
            if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
                raise ValueError(f"{field} deve ser uma lista de nomes de colunas.")
            if len(set(values)) != len(values):
                raise ValueError(f"Colunas repetidas em {field}.")
        if spec["columns"] != "*" and set(spec["key"]) & set(spec["columns"]):
            raise ValueError("A chave não pode estar na lista de colunas atualizadas.")
        table = (spec["schema"], spec["table"])
        if table in seen:
            raise ValueError("Tabela repetida na configuração.")
        seen.add(table)


def index_rows(rows, keys):
    indexed = {}
    for row in rows:
        key = tuple(row[k] for k in keys)
        if any(k is None for k in key):
            raise ValueError("Encontrado identificador nulo.")
        try:
            if key in indexed:
                raise ValueError("Encontrado identificador duplicado.")
            indexed[key] = row
        except TypeError as exc:
            raise ValueError("O identificador deve conter valores escalares.") from exc
    return indexed


def compare(spec, source, target):
    src = index_rows(source, spec["key"])
    dst = index_rows(target, spec["key"])
    changes, updates = [], []
    missing = 0
    for key, row in src.items():
        if key not in dst:
            missing += 1
            continue
        old = dst[key]
        fields = [c for c in spec["columns"]
                  if not empty_source(row[c]) and not same(row[c], old[c])]
        if fields:
            changes.append({"key": normal(dict(zip(spec["key"], key))),
                            "before": normal({c: old[c] for c in fields}),
                            "after": normal({c: row[c] for c in fields})})
            updates.append((key, fields, row))
    changes.sort(key=lambda item: json.dumps(item["key"], sort_keys=True, ensure_ascii=True))
    untouched = [
        {"key": normal(dict(zip(spec["key"], key))),
         "values": normal({c: row[c] for c in spec["key"] + spec["columns"]})}
        for key, row in dst.items() if key not in src
    ]
    untouched.sort(key=lambda item: json.dumps(item["key"], sort_keys=True, ensure_ascii=True))
    report = {"schema": spec["schema"], "table": spec["table"],
              "columns": spec["columns"],
              "updates": changes, "local_only_skipped": missing,
              "production_only_untouched": len(untouched),
              "production_only_rows": untouched}
    return report, updates


def metadata(conn, spec):
    rows = conn.execute("""
        SELECT column_name, udt_schema, udt_name, is_generated, is_identity
        FROM information_schema.columns WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
    """, (spec["schema"], spec["table"])).fetchall()
    meta = {r["column_name"]: r for r in rows}
    names = spec["key"] if spec["columns"] == "*" else spec["key"] + spec["columns"]
    for name in names:
        if name not in meta:
            raise ValueError(f"Coluna ausente: {spec['schema']}.{spec['table']}.{name}")
    return meta


def resolve_columns(spec, src_meta, dst_meta):
    if spec["columns"] != "*":
        return spec
    if src_meta.keys() != dst_meta.keys():
        raise ValueError(f"As colunas da tabela {spec['table']} diferem entre os bancos.")
    columns = [name for name in src_meta if name not in spec["key"]
               and all(meta[name]["is_generated"] == "NEVER"
                       and meta[name]["is_identity"] == "NO"
                       for meta in (src_meta, dst_meta))]
    if not columns:
        raise ValueError(f"Nenhuma coluna atualizável na tabela {spec['table']}.")
    return dict(spec, columns=columns)


def read_rows(conn, spec, sql):
    query = sql.SQL("SELECT {} FROM {}.{}").format(
        sql.SQL(", ").join(map(sql.Identifier, spec["key"] + spec["columns"])),
        sql.Identifier(spec["schema"]), sql.Identifier(spec["table"]))
    return conn.execute(query).fetchall()


def identity(conn):
    return conn.execute("""SELECT current_database() AS database,
        inet_server_addr()::text AS address, inet_server_port() AS port""").fetchone()


def build_plan(source, target, config, sql):
    origin, destination = identity(source), identity(target)
    if origin == destination:
        raise ValueError("Origem e destino apontam para o mesmo banco.")
    report = {"version": 4, "config": config, "source": origin,
              "destination": destination, "tables": []}
    work = []
    for spec in config["tables"]:
        src_meta, dst_meta = metadata(source, spec), metadata(target, spec)
        spec = resolve_columns(spec, src_meta, dst_meta)
        for name in spec["key"] + spec["columns"]:
            if (src_meta[name]["udt_schema"], src_meta[name]["udt_name"]) != (dst_meta[name]["udt_schema"], dst_meta[name]["udt_name"]):
                raise ValueError(f"Tipos incompatíveis: {spec['table']}.{name}")
        for name in spec["columns"]:
            if dst_meta[name]["is_generated"] != "NEVER" or dst_meta[name]["is_identity"] == "YES":
                raise ValueError(f"Coluna gerada não pode ser atualizada: {name}")
        target_rows = read_rows(target, spec, sql)
        table_report, updates = compare(spec, read_rows(source, spec, sql), target_rows)
        report["tables"].append(table_report)
        expected_rows = index_rows(target_rows, spec["key"])
        for key, fields, row in updates:
            expected_rows[key] = dict(expected_rows[key])
            expected_rows[key].update({c: row[c] for c in fields})
        work.append((spec, updates, dst_meta, expected_rows))
    return report, work


def digest(report):
    content = json.dumps(report, sort_keys=True, ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(content.encode()).hexdigest()


def save_untouched_report(plan, count, path):
    total = sum(t["production_only_untouched"] for t in plan["tables"])
    lines = ["REGISTROS SOMENTE EM PRODUÇÃO — PRESERVADOS",
             f"Registros atualizados em produção: {count}",
             f"Registros não encontrados na origem: {total}",
             "Os registros listados abaixo não foram atualizados nem excluídos.",
             "A identificação usa as chaves configuradas para cada tabela.", ""]
    for table in plan["tables"]:
        lines.append(f"Tabela: {table['schema']}.{table['table']}")
        lines.append(f"Quantidade preservada: {table['production_only_untouched']}")
        for row in table["production_only_rows"]:
            lines.append("Chave: " + json.dumps(row["key"], ensure_ascii=False))
            lines.append("Dados (chave e colunas comparadas):")
            lines.append(json.dumps(row["values"], ensure_ascii=False, indent=2, allow_nan=False))
        if not table["production_only_rows"]:
            lines.append("Nenhum registro somente em produção nesta tabela.")
        lines.append("")
    with Path(path).open("x", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")
    return total


def connection_error_reason(exc):
    """Classify driver diagnostics without disclosing connection strings."""
    message = str(exc).lower()
    state = getattr(exc, "sqlstate", None)
    if state == "28P01" or "password authentication failed" in message or "no password supplied" in message or "autenticação com senha falhou" in message or "nenhuma senha fornecida" in message:
        return "Falha de autenticação. Confira usuário e senha (ou pgpass.conf)."
    if "could not translate host name" in message or "name or service not known" in message or "getaddrinfo" in message:
        return "Servidor não encontrado. Confira o host e o acesso à rede/VPN."
    if "connection refused" in message or "actively refused" in message or "recusou ativamente" in message or "conexão recusada" in message:
        return "Conexão recusada. Confira host, porta e se o PostgreSQL/túnel SSH está ativo."
    if "timeout" in message or "timed out" in message:
        return "Tempo de conexão esgotado. Confira rede, VPN, firewall e porta."
    if "no pg_hba.conf entry" in message:
        return "O servidor não autorizou esta conexão. Confira as regras pg_hba.conf e o SSL exigido."
    if "ssl" in message or "certificate" in message:
        return "Falha de SSL/certificado. Confira sslmode e os certificados da conexão."
    if state == "3D000" or ("database" in message and "does not exist" in message):
        return "Banco de dados não encontrado. Confira dbname."
    if state == "28000":
        return "Acesso não autorizado. Confira usuário e regras de autenticação do servidor."
    return "Não foi possível conectar. Confira host, porta, dbname, usuário, senha, SSL e eventual túnel SSH."


def safe_connection_detail(exc, dsn):
    from psycopg.conninfo import conninfo_to_dict
    try:
        params = conninfo_to_dict(dsn)
    except Exception:
        return "Detalhe indisponível: formato da conexão inválido."
    detail = str(exc)
    secrets = {dsn, params.get("password", ""), os.environ.get("PGPASSWORD", "")}
    variants = set()
    for secret in secrets:
        if secret:
            variants.update((secret, quote(secret, safe=""), quote_plus(secret, safe=""),
                             secret.replace("\\", "\\\\").replace("'", "\\'")))
    # One pass avoids replacing characters inside the redaction marker itself.
    if variants:
        pattern = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
        detail = re.sub(pattern, "[oculto]", detail)
    detail = re.sub(r"(?i)(password\s*=\s*)(?:'[^']*'|\"[^\"]*\"|[^\s]+)", r"\1[oculto]", detail)
    return detail[:2000]


def connect_database(dsn, label):
    import psycopg
    from psycopg.rows import dict_row
    try:
        return psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=10)
    except psycopg.OperationalError as exc:
        raise ValueError(f"Conexão com o banco {label}: {connection_error_reason(exc)}\n"
                         f"Detalhe do PostgreSQL: {safe_connection_detail(exc, dsn)}") from None


def execute_updates(target, work, sql):
    from psycopg.types.json import Json, Jsonb
    count = 0
    for spec, updates, meta, expected_rows in work:
        for key, fields, row in updates:
            values = []
            for name in fields:
                value = row[name]
                kind = meta[name]["udt_name"]
                if value is not None and kind in ("json", "jsonb"):
                    value = (Jsonb if kind == "jsonb" else Json)(value)
                values.append(value)
            query = sql.SQL("UPDATE {}.{} SET {} WHERE {}").format(
                sql.Identifier(spec["schema"]), sql.Identifier(spec["table"]),
                sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in fields),
                sql.SQL(" AND ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in spec["key"]))
            cur = target.execute(query, values + list(key))
            if cur.rowcount != 1:
                raise ValueError("Atualização não atingiu exatamente um registro; transação cancelada.")
            count += 1
    # Detect trigger effects on all selected rows/columns, across all configured tables.
    for spec, updates, meta, expected_rows in work:
        actual = index_rows(read_rows(target, spec, sql), spec["key"])
        if actual.keys() != expected_rows.keys():
            raise ValueError("Registros gravados diferem do plano; transação cancelada.")
        for key, row in expected_rows.items():
            if any(not same(actual[key][c], row[c]) for c in spec["columns"]):
                raise ValueError("Valor gravado difere do plano; transação cancelada.")
    return count


def main():
    parser = argparse.ArgumentParser(description="Atualiza registros existentes: local para produção.")
    parser.add_argument("mode", choices=["compare", "apply"])
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--report", default="relatorio.json")
    parser.add_argument("--approve", help="SHA-256 mostrado pelo comando compare")
    parser.add_argument("--missing-report", help="Arquivo TXT com registros somente em produção, gerado após apply")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    validate(config)
    expected = None
    if args.mode == "apply":
        expected = json.loads(Path(args.report).read_text(encoding="utf-8"))
        if not args.approve or args.approve != digest(expected):
            raise ValueError("Para aplicar, informe --approve com o hash do relatório revisado.")
        missing_path = Path(args.missing_report) if args.missing_report else Path(args.report).with_suffix(".somente-producao.txt")
        if missing_path.exists():
            raise ValueError(f"O TXT já existe: {missing_path}. Use outro nome em --missing-report.")
        if not missing_path.parent.is_dir():
            raise ValueError("A pasta de destino do TXT não existe. Confira --missing-report.")
    source_dsn, target_dsn = os.environ.get("LOCAL_DSN"), os.environ.get("PRODUCTION_DSN")
    if not source_dsn or not target_dsn:
        raise ValueError("Defina LOCAL_DSN e PRODUCTION_DSN nas variáveis de ambiente.")
    from psycopg import sql
    with connect_database(source_dsn, "local") as source:
        with connect_database(target_dsn, "produção") as target:
            with source.transaction(), target.transaction():
                source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                target.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" if args.mode == "compare" else "SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                for conn in (source, target):
                    conn.execute("SET LOCAL statement_timeout = '60s'")
                    conn.execute("SET LOCAL lock_timeout = '5s'")
                if args.mode == "apply":
                    # Lock all tables in a deterministic order before reading and updating.
                    for spec in sorted(config["tables"], key=lambda s: (s["schema"], s["table"])):
                        target.execute(sql.SQL("LOCK TABLE {}.{} IN SHARE ROW EXCLUSIVE MODE").format(sql.Identifier(spec["schema"]), sql.Identifier(spec["table"])))
                plan, work = build_plan(source, target, config, sql)
                if args.mode == "apply":
                    if plan != expected:
                        raise ValueError("Os dados ou a configuração mudaram desde a comparação. Gere e revise um novo relatório.")
                    count = execute_updates(target, work, sql)
                else:
                    count = sum(len(t["updates"]) for t in plan["tables"])
            # Only announce success after COMMIT has completed.
    if args.mode == "compare":
        with Path(args.report).open("x", encoding="utf-8") as file:
            json.dump(plan, file, ensure_ascii=False, indent=2, allow_nan=False)
        print(f"{count} registros com alterações. Relatório: {args.report}")
        print(f"Hash para aprovação: {digest(plan)}")
    else:
        print(f"Concluído: {count} registros atualizados em produção.")
        try:
            total = save_untouched_report(plan, count, missing_path)
        except OSError:
            raise ValueError("As alterações JÁ FORAM CONFIRMADAS em produção, mas não foi possível salvar o TXT. "
                             "Consulte production_only_rows no relatório JSON revisado; não repita a aplicação sem uma nova comparação.") from None
        print(f"{total} registros somente em produção foram preservados. TXT: {missing_path}")


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        # Database errors may contain values or connection details; do not echo them.
        if isinstance(exc, (ValueError, FileNotFoundError, FileExistsError, ModuleNotFoundError)):
            print(f"Erro: {exc}", file=sys.stderr)
        else:
            print(f"Operação cancelada ({type(exc).__name__}). Verifique conexão, permissões e restrições do banco.", file=sys.stderr)
        sys.exit(1)
