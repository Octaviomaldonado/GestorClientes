"""
ProyectoV3.py — Gestor de clientes (SQLite + Typer CLI + Flask Web con Tailwind)

Requisitos:
  pip install typer[all] rich phonenumbers email-validator flask pandas openpyxl xlsxwriter

Uso CLI (con Typer):
  py ProyectoV3.py --help
  py ProyectoV3.py add
  py ProyectoV3.py list
  py ProyectoV3.py export -o clientes.xlsx
  py ProyectoV3.py shell        

Modo Web (local con Flask):
  py ProyectoPruebaV3.py web --host 127.0.0.1 --port 8000 --debug
  O
  py ProyectoV3.py web --host 127.0.0.1 --port 8000 --debug
  
  (abrir http://127.0.0.1:8000)
"""

from __future__ import annotations

import io
import os
import shlex
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat
from email_validator import validate_email, EmailNotValidError

import smtplib

# --- SMTP (leer de variables de entorno) ---
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))  # 587 STARTTLS / 465 SSL
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "no-reply@example.com")

# --- Flask ---
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_file,
)

# --- Configuración básica ---
APP = typer.Typer(add_completion=False, help="Gestor de clientes (SQLite + CLI + Web).")
console = Console()
DEFAULT_REGION = "US"  # Cambiá a "AR", etc. si lo preferís por defecto
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "clientes.db"
LOG_FILE = BASE_DIR / "gestor.log"
TEMPLATES_DIR = BASE_DIR / "templates"

# =========================
# Utilidades / Logging
# =========================

def log(msg: str) -> None:
    """Log mínimo a archivo de texto."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} | {msg}\n")
    except Exception:
        pass

def enviar_email(destino: str, asunto: str, cuerpo: str) -> None:
    cfg = get_smtp_config()  # lee de SQLite y, si falta, cae a env vars
    if not (cfg["host"] and cfg["user"] and cfg["password"]):
        raise RuntimeError("SMTP no configurado. Completá los Ajustes primero.")

    msg = (
        f"From: {cfg['from']}\r\n"
        f"To: {destino}\r\n"
        f"Subject: {asunto}\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=UTF-8\r\n"
        "\r\n"
        f"{cuerpo}"
    ).encode("utf-8")

    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as server:
        server.ehlo()
        if cfg["port"] == 587:
            server.starttls()
            server.ehlo()
        server.login(cfg["user"], cfg["password"])
        server.sendmail(cfg["from"], [destino], msg)

# =========================
# Modelo de dominio
# =========================

@dataclass
class Cliente:
    nombre: str
    apellido: str
    telefono_e164: str
    email: str
    activo: int = 1
    notas: str = ""


# =========================
# Base de datos (SQLite)
# =========================

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as con:
        con.executescript("""
        PRAGMA foreign_keys = ON;

        /* ==== Clientes ==== */
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            apellido TEXT NOT NULL,
            telefono_e164 TEXT NOT NULL,
            email TEXT NOT NULL,
            activo INTEGER NOT NULL DEFAULT 1,
            notas TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_clientes_email ON clientes(email);

        /* ==== Notas ==== */
        CREATE TABLE IF NOT EXISTS notas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente_id INTEGER NOT NULL,
            contenido TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (cliente_id) REFERENCES clientes(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS ix_notas_cliente ON notas(cliente_id, created_at DESC);

        /* ==== Ajustes (SMTP) ==== */
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        /* ==== Turnos ==== */
        CREATE TABLE IF NOT EXISTS turnos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente_id INTEGER,                 
            inicio TEXT NOT NULL,               
            motivo TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (cliente_id) REFERENCES clientes(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS ix_turnos_inicio ON turnos(inicio);
        """)

# =========================
# Validaciones y normalización
# =========================

def validar_y_normalizar_email(email: str) -> str:
    try:
        result = validate_email(email, check_deliverability=True)
        return result.email.lower()
    except EmailNotValidError as e:
        raise ValueError(f"Email inválido: {e}")

def validar_y_normalizar_telefono(telefono: str, region: str = DEFAULT_REGION) -> str:
    try:
        num = phonenumbers.parse(telefono, region)
        if not (
            phonenumbers.is_valid_number(num)
            and phonenumbers.is_valid_number_for_region(num, region)
        ):
            raise ValueError(f"Teléfono inválido para la región {region}.")
        return phonenumbers.format_number(num, PhoneNumberFormat.E164)  # +14155552671
    except NumberParseException as e:
        raise ValueError(f"Teléfono inválido: {e}")

# =========================
# Turnos
# =========================

from datetime import datetime as _dt  # alias para evitar confusión local

def _combine_fecha_hora(fecha: str, hora: str) -> str:
    # Valida y devuelve "YYYY-MM-DD HH:MM"
    try:
        dt = _dt.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M")
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("Fecha u hora inválida. Formatos esperados: AAAA-MM-DD y HH:MM")

def repo_turnos_add(cliente_id: Optional[int], fecha: str, hora: str, motivo: str) -> int:
    inicio = _combine_fecha_hora(fecha, hora)
    now = _dt.now().isoformat(timespec="seconds")
    with get_conn() as con:
        cur = con.execute(
            "INSERT INTO turnos (cliente_id, inicio, motivo, created_at) VALUES (?, ?, ?, ?)",
            (cliente_id, inicio, motivo.strip(), now),
        )
        return int(cur.lastrowid)

def repo_turnos_list(futuro: Optional[bool] = None) -> List[sqlite3.Row]:
    now = _dt.now().strftime("%Y-%m-%d %H:%M")
    where, params, order = "", [], "t.inicio ASC"
    if futuro is True:
        where, params = "WHERE t.inicio >= ?", [now]
        order = "t.inicio ASC"
    elif futuro is False:
        where, params = "WHERE t.inicio < ?", [now]
        order = "t.inicio DESC"

    with get_conn() as con:
        cur = con.execute(
            f"""
            SELECT t.id, t.inicio, t.motivo, t.cliente_id,
                   c.nombre, c.apellido, c.email
            FROM turnos t
            LEFT JOIN clientes c ON c.id = t.cliente_id
            {where}
            ORDER BY {order}
            """,
            params,
        )
        return list(cur.fetchall())

def repo_turnos_get(turno_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as con:
        cur = con.execute(
            """
            SELECT t.id, t.inicio, t.motivo, t.cliente_id,
                   c.nombre, c.apellido, c.email
            FROM turnos t
            LEFT JOIN clientes c ON c.id = t.cliente_id
            WHERE t.id = ?
            """,
            (turno_id,),
        )
        return cur.fetchone()

def repo_turnos_update(turno_id: int, *, cliente_id: Optional[int], fecha: str, hora: str, motivo: str) -> bool:
    inicio = _combine_fecha_hora(fecha, hora)
    with get_conn() as con:
        cur = con.execute(
            "UPDATE turnos SET cliente_id = ?, inicio = ?, motivo = ? WHERE id = ?",
            (cliente_id, inicio, motivo.strip(), turno_id),
        )
        return cur.rowcount > 0

def repo_turnos_delete(turno_id: int) -> bool:
    with get_conn() as con:
        cur = con.execute("DELETE FROM turnos WHERE id = ?", (turno_id,))
        return cur.rowcount > 0

# =========================
# Repositorio (CRUD)
# =========================

def repo_add(c: Cliente) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as con:
        cur = con.execute(
            """
            INSERT INTO clientes (nombre, apellido, telefono_e164, email, activo, notas, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (c.nombre, c.apellido, c.telefono_e164, c.email, c.activo, c.notas, now, now),
        )
        return int(cur.lastrowid)

def repo_list(activo: Optional[bool] = None) -> List[sqlite3.Row]:
    where = ""
    if activo is True:
        where = "WHERE activo = 1"
    elif activo is False:
        where = "WHERE activo = 0"
    with get_conn() as con:
        cur = con.execute(
            f"""
            SELECT id, nombre, apellido, telefono_e164 AS telefono, email, activo, notas, created_at, updated_at
            FROM clientes
            {where}
            ORDER BY apellido COLLATE NOCASE, nombre COLLATE NOCASE
            """
        )
        return list(cur.fetchall())

def repo_get_by_email(email: str) -> Optional[sqlite3.Row]:
    with get_conn() as con:
        cur = con.execute(
            """
            SELECT id, nombre, apellido, telefono_e164, email, activo, notas, created_at, updated_at
            FROM clientes
            WHERE email = ?
            """,
            (email,),
        )
        return cur.fetchone()

def repo_notas_list(cliente_id: Optional[int] = None) -> List[sqlite3.Row]:
    where = "WHERE n.cliente_id = ?" if cliente_id else ""
    params = (cliente_id,) if cliente_id else ()
    with get_conn() as con:
        cur = con.execute(
            f"""
            SELECT n.id, n.cliente_id, n.contenido, n.created_at,
                   c.nombre, c.apellido, c.email
            FROM notas n
            JOIN clientes c ON c.id = n.cliente_id
            {where}
            ORDER BY n.created_at DESC
            """,
            params,
        )
        return list(cur.fetchall())

def repo_notas_add(cliente_id: int, contenido: str) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as con:
        cur = con.execute(
            "INSERT INTO notas (cliente_id, contenido, created_at) VALUES (?, ?, ?)",
            (cliente_id, contenido.strip(), now),
        )
        return int(cur.lastrowid)

def repo_notas_delete(nota_id: int) -> bool:
    with get_conn() as con:
        cur = con.execute("DELETE FROM notas WHERE id = ?", (nota_id,))
        return cur.rowcount > 0

def repo_get_by_id(cliente_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as con:
        cur = con.execute(
            """
            SELECT id, nombre, apellido, telefono_e164 AS telefono, email, activo, notas, created_at, updated_at
            FROM clientes
            WHERE id = ?
            """,
            (cliente_id,),
        )
        return cur.fetchone()

def repo_update_by_id(
    cliente_id: int,
    *,
    nombre: Optional[str] = None,
    apellido: Optional[str] = None,
    telefono_e164: Optional[str] = None,
    email_nuevo: Optional[str] = None,
    activo: Optional[bool] = None,
    notas: Optional[str] = None,
) -> bool:
    fields = []
    params: List[object] = []

    if nombre is not None:
        fields.append("nombre = ?")
        params.append(nombre)
    if apellido is not None:
        fields.append("apellido = ?")
        params.append(apellido)
    if telefono_e164 is not None:
        fields.append("telefono_e164 = ?")
        params.append(telefono_e164)
    if email_nuevo is not None:
        fields.append("email = ?")
        params.append(email_nuevo)
    if activo is not None:
        fields.append("activo = ?")
        params.append(1 if activo else 0)
    if notas is not None:
        fields.append("notas = ?")
        params.append(notas)

    if not fields:
        return False

    fields.append("updated_at = ?")
    params.append(datetime.now().isoformat(timespec="seconds"))
    params.append(cliente_id)

    with get_conn() as con:
        cur = con.execute(
            f"UPDATE clientes SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        return cur.rowcount > 0

def repo_delete_by_id(cliente_id: int) -> bool:
    with get_conn() as con:
        cur = con.execute("DELETE FROM clientes WHERE id = ?", (cliente_id,))
        return cur.rowcount > 0

def repo_update(
    email_original: str,
    *,
    nombre: Optional[str] = None,
    apellido: Optional[str] = None,
    telefono_e164: Optional[str] = None,
    email_nuevo: Optional[str] = None,
    activo: Optional[bool] = None,
    notas: Optional[str] = None,
) -> bool:
    fields = []
    params: List[object] = []

    if nombre is not None:
        fields.append("nombre = ?")
        params.append(nombre)
    if apellido is not None:
        fields.append("apellido = ?")
        params.append(apellido)
    if telefono_e164 is not None:
        fields.append("telefono_e164 = ?")
        params.append(telefono_e164)
    if email_nuevo is not None:
        fields.append("email = ?")
        params.append(email_nuevo)
    if activo is not None:
        fields.append("activo = ?")
        params.append(1 if activo else 0)
    if notas is not None:
        fields.append("notas = ?")
        params.append(notas)

    if not fields:
        return False  # nada para actualizar

    fields.append("updated_at = ?")
    params.append(datetime.now().isoformat(timespec="seconds"))
    params.append(email_original)

    with get_conn() as con:
        cur = con.execute(
            f"UPDATE clientes SET {', '.join(fields)} WHERE email = ?",
            params,
        )
        return cur.rowcount > 0

def repo_delete(email: str) -> bool:
    with get_conn() as con:
        cur = con.execute("DELETE FROM clientes WHERE email = ?", (email,))
        return cur.rowcount > 0

# =========================
# Ajustes (SMTP en SQLite)
# =========================
def set_setting(key: str, value: str) -> None:
    with get_conn() as con:
        con.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

def get_settings() -> dict:
    with get_conn() as con:
        cur = con.execute("SELECT key, value FROM settings")
        data = {row["key"]: row["value"] for row in cur.fetchall()}
    return data

def get_smtp_config() -> dict:
    """Prioriza lo guardado en SQLite y cae a variables de entorno si falta algo."""
    s = get_settings()
    return {
        "host": s.get("SMTP_HOST") or os.getenv("SMTP_HOST", ""),
        "port": int(s.get("SMTP_PORT") or os.getenv("SMTP_PORT", "587")),
        "user": s.get("SMTP_USER") or os.getenv("SMTP_USER", ""),
        "password": s.get("SMTP_PASS") or os.getenv("SMTP_PASS", ""),
        "from": s.get("SMTP_FROM") or os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "")) or "no-reply@example.com",
    }

# =========================
# Presentación (Rich)
# =========================

def print_table(rows: List[sqlite3.Row]) -> None:
    table = Table(title=f"Clientes ({len(rows)})", show_lines=False)
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Apellido")
    table.add_column("Nombre")
    table.add_column("Teléfono")
    table.add_column("Email")
    table.add_column("Activo")
    table.add_column("Notas", overflow="fold")
    for r in rows:
        table.add_row(
            str(r["id"]),
            r["apellido"],
            r["nombre"],
            r["telefono"],
            r["email"],
            "✔" if r["activo"] else "✖",
            (r["notas"] or ""),
        )
    console.print(table)

# =========================
# CLI (Typer)
# =========================

@APP.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    """Inicializa la base si es primera ejecución y muestra ayuda si no hay comando."""
    init_db()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())

@APP.command("add")
def cli_add(
    nombre: Optional[str] = typer.Option(None, prompt=True),
    apellido: Optional[str] = typer.Option(None, prompt=True),
    telefono: Optional[str] = typer.Option(None, prompt=True),
    email: Optional[str] = typer.Option(None, prompt=True),
    region: str = typer.Option(DEFAULT_REGION, help="Región para validar el teléfono (US, AR, etc.)"),
    notas: str = typer.Option("", help="Notas opcionales"),
):
    """Agrega un cliente validando email y teléfono, y normaliza a E.164."""
    init_db()
    try:
        email_norm = validar_y_normalizar_email(email)
        tel_e164 = validar_y_normalizar_telefono(telefono, region=region.upper())
        cliente = Cliente(
            nombre=nombre.strip(),
            apellido=apellido.strip(),
            telefono_e164=tel_e164,
            email=email_norm,
            notas=notas.strip(),
        )
        new_id = repo_add(cliente)
        console.print(f"[bold green]Cliente agregado (id={new_id}).[/bold green]")
        log(f"ADD {email_norm}")
    except sqlite3.IntegrityError:
        console.print(f"[bold red]El email ya existe:[/bold red] {email}")
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")

@APP.command("list")
def cli_list(
    activos: bool = typer.Option(False, "--activos", help="Mostrar solo activos"),
    inactivos: bool = typer.Option(False, "--inactivos", help="Mostrar solo inactivos"),
):
    """Lista clientes (todos por defecto)."""
    init_db()
    filtro: Optional[bool]
    if activos and inactivos:
        filtro = None  # se cancelan
    elif activos:
        filtro = True
    elif inactivos:
        filtro = False
    else:
        filtro = None

    rows = repo_list(filtro)
    print_table(rows)

@APP.command("update")
def cli_update(
    email: str = typer.Option(..., prompt="Email (actual)"),
    nombre: Optional[str] = typer.Option(None),
    apellido: Optional[str] = typer.Option(None),
    telefono: Optional[str] = typer.Option(None, help="Nuevo teléfono (se validará)"),
    region: str = typer.Option(DEFAULT_REGION, help="Región para validar el teléfono si se cambia"),
    email_nuevo: Optional[str] = typer.Option(None, help="Nuevo email (se validará)"),
    activo: Optional[bool] = typer.Option(None, help="True/False"),
    notas: Optional[str] = typer.Option(None),
):
    """Modifica campos del cliente identificado por email actual."""
    init_db()
    try:
        email_original = validar_y_normalizar_email(email)
        tel_e164 = None
        email_final = None

        if telefono is not None:
            tel_e164 = validar_y_normalizar_telefono(telefono, region=region.upper())
        if email_nuevo is not None:
            email_final = validar_y_normalizar_email(email_nuevo)

        ok = repo_update(
            email_original,
            nombre=nombre.strip() if isinstance(nombre, str) else None,
            apellido=apellido.strip() if isinstance(apellido, str) else None,
            telefono_e164=tel_e164,
            email_nuevo=email_final,
            activo=activo,
            notas=notas.strip() if isinstance(notas, str) else None,
        )
        if ok:
            console.print("[bold green]Cliente actualizado.[/bold green]")
            log(f"UPDATE {email_original}")
        else:
            console.print("[yellow]No hubo cambios o no se encontró el cliente.[/yellow]")
    except sqlite3.IntegrityError:
        console.print("[bold red]El email nuevo ya existe en otro cliente.[/bold red]")
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")

@APP.command("delete")
def cli_delete(email: str = typer.Option(..., prompt=True), force: bool = typer.Option(False, "--force")):
    """Elimina un cliente por email."""
    init_db()
    try:
        email_norm = validar_y_normalizar_email(email)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    if not force:
        confirmar = typer.confirm(f"¿Eliminar definitivamente {email_norm}?")
        if not confirmar:
            console.print("[yellow]Cancelado.[/yellow]")
            raise typer.Exit()

    ok = repo_delete(email_norm)
    if ok:
        console.print("[bold green]Cliente eliminado.[/bold green]")
        log(f"DELETE {email_norm}")
    else:
        console.print("[yellow]No se encontró el cliente.[/yellow]")

@APP.command("export")
def cli_export(path: Path = typer.Option(Path("clientes.xlsx"), "--path", "-o", help="Ruta de salida XLSX")):
    """Exporta la lista de clientes a Excel."""
    init_db()
    rows = repo_list(None)
    if not rows:
        console.print("[yellow]No hay clientes para exportar.[/yellow]")
        raise typer.Exit()

    try:
        import pandas as pd  # type: ignore

        data = [
            {
                "id": r["id"],
                "nombre": r["nombre"],
                "apellido": r["apellido"],
                "telefono": r["telefono"],
                "email": r["email"],
                "activo": r["activo"],
                "notas": r["notas"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
        df = pd.DataFrame(data)

        try:
            import xlsxwriter  # noqa: F401
            engine = "xlsxwriter"
        except Exception:
            engine = None

        if engine:
            with pd.ExcelWriter(path, engine=engine) as writer:
                df.to_excel(writer, sheet_name="Clientes", index=False, startrow=1, header=False)
                wb = writer.book
                ws = writer.sheets["Clientes"]

                header_fmt = wb.add_format({"bold": True, "bg_color": "#DDEBF7", "border": 1})
                for col, name in enumerate(df.columns):
                    ws.write(0, col, name, header_fmt)
                    col_len = max(len(str(name)), int(df[name].astype(str).map(len).max()))
                    ws.set_column(col, col, min(col_len + 2, 50))

                text_fmt = wb.add_format({"num_format": "@"})
                tel_col = df.columns.get_loc("telefono")
                ws.set_column(tel_col, tel_col, None, text_fmt)

                ws.freeze_panes(1, 0)
                ws.autofilter(0, 0, len(df), len(df.columns) - 1)
        else:
            df.to_excel(path, index=False, sheet_name="Clientes")

        console.print(f"[bold green]Exportado:[/bold green] {path.resolve()}")
        log(f"EXPORT {path.resolve()}")
    except ImportError:
        console.print("[bold red]Necesitás instalar pandas para exportar: pip install pandas openpyxl xlsxwriter[/bold red]")

@APP.command("get")
def cli_get(email: str = typer.Option(..., prompt=True)):
    """Muestra un cliente por email."""
    init_db()
    try:
        email_norm = validar_y_normalizar_email(email)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    row = repo_get_by_email(email_norm)
    if not row:
        console.print("[yellow]No se encontró el cliente.[/yellow]")
        raise typer.Exit()

    table = Table(title="Cliente")
    for col in row.keys():
        table.add_row(col, str(row[col]))
    console.print(table)

@APP.command("shell")
def cli_shell():
    """
    Modo interactivo: escribí comandos como 'add', 'list', 'get --email ...',
    'update --email ...', 'delete --email ...', 'export -o clientes.xlsx'.
    Usá 'help' para ver ayuda y 'exit' para salir.
    """
    init_db()
    console.print("[bold]Modo interactivo.[/bold] Escribí [green]help[/green] para ayuda, [red]exit[/red] para salir.")
    while True:
        try:
            raw = input("gestor> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break

        if not raw:
            continue

        cmd = raw.lower()
        if cmd in {"exit", "quit", "salir"}:
            break
        if cmd in {"help", "ayuda", "?"}:
            try:
                APP(standalone_mode=False, args=["--help"])
            except SystemExit:
                pass
            continue

        try:
            args = shlex.split(raw)
            APP(standalone_mode=False, args=args)
        except SystemExit:
            pass
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")

# =========================
# Web (Flask + Tailwind)
# =========================

app = Flask(__name__)
app.secret_key = "change-me"  # para flash messages

def ensure_templates() -> None:
    """
    Crea los templates por primera vez únicamente si no existen.
    NO sobrescribe archivos ya presentes (para no pisar cambios).
    """
    tdir = TEMPLATES_DIR
    tdir.mkdir(exist_ok=True)

    def write_if_missing(path: Path, content: str) -> None:
        if not path.exists():
            path.write_text(content, encoding="utf-8")

    # base.html (placeholder mínimo por si falta; NO se pisa si ya lo tenés)
    write_if_missing(
        tdir / "base.html",
        """<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{% block title %}Gestor de Clientes{% endblock %}</title>
    <style>
      :root { --bg:#f8fafc; --fg:#0f172a; --muted:#64748b; --card:#ffffff; --border:#e2e8f0; }
      body { background: var(--bg); color: var(--fg); font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin:0; }
      .container { max-width: 72rem; margin-inline: auto; padding: 1rem; }
      .nav { background:#fff; border-bottom:1px solid var(--border); position: sticky; top:0; z-index:10; }
      .btn { padding:.45rem .8rem; border-radius:.5rem; font-size:.9rem; text-decoration:none; display:inline-block }
      .btn-dark { background:#0f172a; color:#fff }
      .btn-indigo { background:#4f46e5; color:#fff }
      .btn-emerald { background:#059669; color:#fff }
      .btn-amber { background:#b45309; color:#fff }
      .btn-red { background:#b91c1c; color:#fff }
      .btn-ghost { background:#eef2ff; color:#1e1b4b }
      .space-x-2>*+*{ margin-left:.5rem }
      .card { background:#fff; border:1px solid var(--border); border-radius:.8rem; box-shadow:0 1px 2px rgba(0,0,0,.04) }
      .table { width:100%; border-collapse:collapse; background:#fff; }
      .table th, .table td { padding:.6rem .8rem; border-top:1px solid var(--border); text-align:left }
      .table thead { background:#f1f5f9; color:#334155 }
      .badge { font-size:.75rem; padding:.1rem .4rem; border-radius:.4rem }
      .ok { background:#d1fae5; color:#065f46 }
      .muted { color:var(--muted) }
      input, select, textarea { width:100%; padding:.55rem .7rem; border:1px solid var(--border); border-radius:.6rem }
      .row { display:grid; gap:1rem; grid-template-columns:1fr }
      @media (min-width:640px){ .row { grid-template-columns:repeat(2, 1fr)} }
      .mt-2{ margin-top:.5rem } .mt-4{ margin-top:1rem } .mb-4{ margin-bottom:1rem }
      .flex{ display:flex } .items-center{ align-items:center } .justify-between{ justify-content:space-between }
    </style>
  </head>
  <body>
    <nav class="nav">
      <div class="container flex items-center justify-between" style="padding:.75rem 1rem">
        <a href="{{ url_for('landing') }}" style="font-weight:600;color:#0f172a;text-decoration:none">Gestor</a>
        <div class="space-x-2">
          <a href="{{ url_for('landing') }}" class="btn btn-ghost">Inicio</a>
          <a href="{{ url_for('clientes') }}" class="btn btn-ghost">Clientes</a>
          <a href="{{ url_for('notas') }}" class="btn btn-ghost">Notas</a>
          <a href="{{ url_for('correo') }}" class="btn btn-ghost">Correo</a>
          <a href="{{ url_for('new_cliente') }}" class="btn btn-indigo">Nuevo</a>
          <a href="{{ url_for('exportar_menu') }}" class="btn btn-emerald">Exportar</a>
        </div>
      </div>
    </nav>

    <main class="container">
      {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
          <div class="mt-2">
            {% for category, msg in messages %}
              <div class="card"
                   style="padding:.6rem .8rem; margin-bottom:.5rem;
                          {% if category=='success' %}background:#ecfdf5;color:#065f46{% else %}background:#fee2e2;color:#7f1d1d{% endif %}">
                {{ msg }}
              </div>
            {% endfor %}
          </div>
        {% endif %}
      {% endwith %}

      {% block content %}{% endblock %}
    </main>
  </body>
</html>
""",
    )

    # landing.html (placeholder mínimo; NO se pisa si ya lo tenés)
    write_if_missing(
        tdir / "landing.html",
        """{% extends 'base.html' %}
{% block title %}Inicio{% endblock %}
{% block content %}
  <div class="card" style="padding:1.2rem">
    <h1 style="margin:0 0 .5rem 0">Bienvenido 👋</h1>
    <p class="muted">Elegí una opción:</p>
    <div class="mt-4">
      <a class="btn btn-dark" href="{{ url_for('clientes') }}">Ir a Clientes</a>
      <a class="btn" href="{{ url_for('notas') }}">Ver Notas</a>
      <a class="btn" href="{{ url_for('correo') }}">Enviar Correo</a>
    </div>
  </div>
{% endblock %}
""",
    )

    # El resto por si faltan (placeholders básicos)
    write_if_missing(tdir / "index.html",   "{% extends 'base.html' %}\n{% block content %}<p>Clientes</p>{% endblock %}\n")
    write_if_missing(tdir / "form.html",    "{% extends 'base.html' %}\n{% block content %}<p>Formulario</p>{% endblock %}\n")
    write_if_missing(tdir / "notas.html",   "{% extends 'base.html' %}\n{% block content %}<p>Notas</p>{% endblock %}\n")
    write_if_missing(tdir / "correo.html",  "{% extends 'base.html' %}\n{% block content %}<p>Correo</p>{% endblock %}\n")
    write_if_missing(tdir / "ajustes.html", "{% extends 'base.html' %}\n{% block content %}<p>Ajustes SMTP</p>{% endblock %}\n")
    # NUEVO: placeholder para el menú de exportación
    write_if_missing(
        tdir / "exportar.html",
        """{% extends 'base.html' %}
{% block title %}Exportar{% endblock %}
{% block content %}
  <div class="card" style="padding:1rem">
    <h2 style="margin:.25rem 0 1rem 0">Exportar datos</h2>
    <div class="row" style="grid-template-columns:repeat(2,1fr)">
      <a class="card" href="{{ url_for('exportar_clientes') }}" style="padding:1rem; text-decoration:none; color:inherit">
        <h3 style="margin:.2rem 0">Clientes</h3>
        <p class="muted" style="margin:0">Descargá un XLSX con todos los clientes.</p>
      </a>
      <a class="card" href="{{ url_for('exportar_turnos') }}" style="padding:1rem; text-decoration:none; color:inherit">
        <h3 style="margin:.2rem 0">Turnos</h3>
        <p class="muted" style="margin:0">Descargá un XLSX con todos los turnos.</p>
      </a>
    </div>
  </div>
{% endblock %}
"""
    )

# ==== Rutas ====

# Lista de clientes -> ahora en /clientes
@app.route("/clientes")
def clientes():
    init_db()
    q = (request.args.get("q") or "").strip().lower()
    solo_activos = bool(request.args.get("solo_activos"))

    rows = repo_list(True if solo_activos else None)

    if q:
        def matches(r: sqlite3.Row) -> bool:
            return (
                q in (r["nombre"] or "").lower()
                or q in (r["apellido"] or "").lower()
                or q in (r["email"] or "").lower()
                or q in (r["telefono"] or "").lower()
            )
        rows = [r for r in rows if matches(r)]

    # convertir a objetos simples para Jinja
    class RowObj:
        def __init__(self, r: sqlite3.Row):
            self.id = r["id"]
            self.nombre = r["nombre"]
            self.apellido = r["apellido"]
            self.telefono = r["telefono"]
            self.email = r["email"]
            self.activo = bool(r["activo"])
            self.notas = r["notas"]
    clientes = [RowObj(r) for r in rows]

    return render_template("index.html", clientes=clientes)

# Landing / portada en "/"
@app.route("/")
def landing():
    init_db()
    # KPIs
    total = len(repo_list(None))
    activos = len(repo_list(True))
    inactivos = len(repo_list(False))
    notas_total = len(repo_notas_list(None))

    # últimas 5 notas
    recientes = repo_notas_list(None)[:5]

    # estado de SMTP
    try:
        cfg = get_smtp_config()  # si no lo tenés, ya te lo pasé antes
        smtp_ok = bool(cfg["host"] and cfg["user"] and cfg["password"])
    except NameError:
        smtp_ok = False

    stats = {
        "total": total,
        "activos": activos,
        "inactivos": inactivos,
        "notas": notas_total,
        "smtp_ok": smtp_ok,
    }
    return render_template("landing.html", stats=stats, recientes=recientes)

@app.route("/ajustes", methods=["GET", "POST"])
def ajustes():
    init_db()
    if request.method == "POST":
        set_setting("SMTP_HOST", (request.form.get("SMTP_HOST") or "").strip())
        set_setting("SMTP_PORT", (request.form.get("SMTP_PORT") or "587").strip())
        set_setting("SMTP_USER", (request.form.get("SMTP_USER") or "").strip())
        set_setting("SMTP_PASS", (request.form.get("SMTP_PASS") or "").strip())
        set_setting("SMTP_FROM", (request.form.get("SMTP_FROM") or "").strip())
        flash("Ajustes guardados.", "success")
        return redirect(url_for("ajustes"))

    cfg = get_smtp_config()
    return render_template("ajustes.html", cfg=cfg)

@app.route("/clientes/nuevo", methods=["GET", "POST"])
def new_cliente():
    init_db()
    form = {
        "nombre": request.form.get("nombre", ""),
        "apellido": request.form.get("apellido", ""),
        "telefono": request.form.get("telefono", ""),
        "email": request.form.get("email", ""),
        "region": request.form.get("region", DEFAULT_REGION),
        "activo": bool(request.form.get("activo")),
        "notas": request.form.get("notas", ""),
    }
    if request.method == "POST":
        try:
            email_norm = validar_y_normalizar_email(form["email"])  # normaliza
            tel_e164 = validar_y_normalizar_telefono(form["telefono"], region=form["region"].upper())
            cliente = Cliente(
                nombre=form["nombre"].strip(),
                apellido=form["apellido"].strip(),
                telefono_e164=tel_e164,
                email=email_norm,
                activo=1 if form["activo"] else 0,
                notas=form["notas"].strip(),
            )
            repo_add(cliente)
            flash("Cliente creado.", "success")
            return redirect(url_for("clientes"))
        except sqlite3.IntegrityError:
            flash("El email ya existe.", "error")
        except ValueError as e:
            flash(str(e), "error")

    return render_template("form.html", titulo="Nuevo cliente", form=form)

@app.route("/clientes/<int:cliente_id>/editar", methods=["GET", "POST"])
def edit_cliente(cliente_id: int):
    init_db()
    row = repo_get_by_id(cliente_id)
    if not row:
        flash("Cliente no encontrado.", "error")
        return redirect(url_for("clientes"))

    if request.method == "POST":
        form = {
            "nombre": request.form.get("nombre", row["nombre"]).strip(),
            "apellido": request.form.get("apellido", row["apellido"]).strip(),
            "telefono": request.form.get("telefono", row["telefono"]),
            "email": request.form.get("email", row["email"]).strip(),
            "region": request.form.get("region", DEFAULT_REGION),
            "activo": bool(request.form.get("activo")),
            "notas": request.form.get("notas", row["notas"] or ""),
        }
        try:
            email_norm = validar_y_normalizar_email(form["email"]) if form["email"] else row["email"]
            tel_e164 = validar_y_normalizar_telefono(form["telefono"], region=form["region"].upper()) if form["telefono"] else row["telefono"]
            ok = repo_update_by_id(
                cliente_id,
                nombre=form["nombre"],
                apellido=form["apellido"],
                telefono_e164=tel_e164,
                email_nuevo=email_norm,
                activo=form["activo"],
                notas=form["notas"],
            )
            if ok:
                flash("Cliente actualizado.", "success")
            else:
                flash("Sin cambios.", "error")
            return redirect(url_for("clientes"))
        except sqlite3.IntegrityError:
            flash("El email nuevo ya existe en otro cliente.", "error")
        except ValueError as e:
            flash(str(e), "error")

    # GET -> prellenar
    form = {
        "nombre": row["nombre"],
        "apellido": row["apellido"],
        "telefono": row["telefono"],
        "email": row["email"],
        "region": DEFAULT_REGION,
        "activo": bool(row["activo"]),
        "notas": row["notas"] or "",
    }
    return render_template("form.html", titulo="Editar cliente", form=form)

@app.route("/clientes/<int:cliente_id>/eliminar", methods=["POST"])
def delete_cliente(cliente_id: int):
    init_db()
    if repo_delete_by_id(cliente_id):
        flash("Cliente eliminado.", "success")
    else:
        flash("No se encontró el cliente.", "error")
    return redirect(url_for("clientes"))

@app.route("/clientes/<int:cliente_id>/toggle", methods=["POST"])
def toggle_cliente(cliente_id: int):
    init_db()
    row = repo_get_by_id(cliente_id)
    if not row:
        flash("Cliente no encontrado.", "error")
        return redirect(url_for("clientes"))
    nuevo = 0 if row["activo"] else 1
    repo_update_by_id(cliente_id, activo=bool(nuevo))
    flash("Estado actualizado.", "success")
    return redirect(url_for("clientes"))

@app.route("/notas", methods=["GET", "POST"])
def notas():
    init_db()
    # Para crear nota rápida desde esta página
    if request.method == "POST":
        cliente_id = int(request.form.get("cliente_id", "0") or "0")
        contenido = (request.form.get("contenido") or "").strip()
        if not cliente_id or not contenido:
            flash("Completá cliente y contenido.", "error")
        else:
            repo_notas_add(cliente_id, contenido)
            flash("Nota creada.", "success")
        return redirect(url_for("notas"))

    # Listado y selector
    rows_clientes = repo_list(None)
    notas_rows = repo_notas_list(None)
    return render_template("notas.html", clientes=rows_clientes, notas=notas_rows)

@app.route("/notas/<int:nota_id>/eliminar", methods=["POST"])
def notas_eliminar(nota_id: int):
    init_db()
    if repo_notas_delete(nota_id):
        flash("Nota eliminada.", "success")
    else:
        flash("No se encontró la nota.", "error")
    return redirect(url_for("notas"))

@app.route("/correo", methods=["GET", "POST"])
def correo():
    init_db()
    # Para prefijar email al entrar con ?cliente_id=...
    pref_cliente_id = request.args.get("cliente_id")
    pref_email = ""
    if pref_cliente_id:
        row = repo_get_by_id(int(pref_cliente_id))
        if row:
            pref_email = row["email"]

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        asunto = (request.form.get("asunto") or "").strip()
        cuerpo = (request.form.get("cuerpo") or "").strip()
        try:
            destino = validar_y_normalizar_email(email)
            if not asunto or not cuerpo:
                raise ValueError("Asunto y cuerpo son obligatorios.")
            enviar_email(destino, asunto, cuerpo)
            flash("Correo enviado.", "success")
            return redirect(url_for("correo"))
        except Exception as e:
            flash(f"Error al enviar: {e}", "error")

   # siempre pasar lista de clientes para elegir rápido
    rows_clientes = repo_list(None)
    cfg = get_smtp_config()
    smtp_ok = bool(cfg["host"] and cfg["user"] and cfg["password"])
    return render_template("correo.html", clientes=rows_clientes, pref_email=pref_email, smtp_ok=smtp_ok)

# ----------- EXPORTACIÓN (WEB) -----------

# Menú de exportación (dos opciones)
@app.route("/exportar/opciones")
@app.route("/export")
def exportar_menu():
    init_db()
    return render_template("exportar.html")

# Exportar CLIENTES (alias del endpoint antiguo /exportar)
@app.route("/exportar/clientes")
def exportar_clientes():
    return exportar()  # reutilizamos la función existente

# Exportar TURNOS
@app.route("/exportar/turnos")
def exportar_turnos():
    init_db()
    rows = repo_turnos_list(None)
    if not rows:
        flash("No hay turnos para exportar.", "error")
        return redirect(url_for("turnos"))

    try:
        import pandas as pd  # type: ignore

        data = []
        for r in rows:
            cliente_nombre = ""
            if r["apellido"] or r["nombre"]:
                cliente_nombre = f"{r['apellido'] or ''}, {r['nombre'] or ''}".strip(", ").strip()
            data.append({
                "id": r["id"],
                "inicio": r["inicio"],
                "motivo": r["motivo"],
                "cliente_id": r["cliente_id"] or "",
                "cliente_nombre": cliente_nombre,
                "cliente_email": r["email"] or "",
            })

        df = pd.DataFrame(data)
        buffer = io.BytesIO()

        try:
            import xlsxwriter  # noqa: F401
            engine = "xlsxwriter"
        except Exception:
            engine = None

        if engine:
            with pd.ExcelWriter(buffer, engine=engine) as writer:
                df.to_excel(writer, sheet_name="Turnos", index=False, startrow=1, header=False)
                wb = writer.book
                ws = writer.sheets["Turnos"]
                header_fmt = wb.add_format({"bold": True, "bg_color": "#DDEBF7", "border": 1})
                for col, name in enumerate(df.columns):
                    ws.write(0, col, name, header_fmt)
                    col_len = max(len(str(name)), int(df[name].astype(str).map(len).max()))
                    ws.set_column(col, col, min(col_len + 2, 50))
                ws.freeze_panes(1, 0)
                ws.autofilter(0, 0, len(df), len(df.columns) - 1)
        else:
            df.to_excel(buffer, index=False, sheet_name="Turnos")

        buffer.seek(0)
        fname = f"turnos_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return send_file(
            buffer,
            as_attachment=True,
            download_name=fname,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except ImportError:
        flash("Necesitás instalar pandas para exportar.", "error")
        return redirect(url_for("turnos"))

# Exportar CLIENTES (endpoint legado /exportar)
@app.route("/exportar")
def exportar():
    init_db()
    rows = repo_list(None)
    if not rows:
        flash("No hay clientes para exportar.", "error")
        return redirect(url_for("clientes"))

    try:
        import pandas as pd  # type: ignore

        data = [
            {
                "id": r["id"],
                "nombre": r["nombre"],
                "apellido": r["apellido"],
                "telefono": r["telefono"],
                "email": r["email"],
                "activo": r["activo"],
                "notas": r["notas"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
        df = pd.DataFrame(data)
        buffer = io.BytesIO()

        try:
            import xlsxwriter  # noqa: F401
            engine = "xlsxwriter"
        except Exception:
            engine = None

        if engine:
            with pd.ExcelWriter(buffer, engine=engine) as writer:
                df.to_excel(writer, sheet_name="Clientes", index=False, startrow=1, header=False)
                wb = writer.book
                ws = writer.sheets["Clientes"]
                header_fmt = wb.add_format({"bold": True, "bg_color": "#DDEBF7", "border": 1})
                for col, name in enumerate(df.columns):
                    ws.write(0, col, name, header_fmt)
                    col_len = max(len(str(name)), int(df[name].astype(str).map(len).max()))
                    ws.set_column(col, col, min(col_len + 2, 50))
                text_fmt = wb.add_format({"num_format": "@"})
                tel_col = df.columns.get_loc("telefono")
                ws.set_column(tel_col, tel_col, None, text_fmt)
                ws.freeze_panes(1, 0)
                ws.autofilter(0, 0, len(df), len(df.columns) - 1)
        else:
            # fallback simple
            df.to_excel(buffer, index=False, sheet_name="Clientes")
        buffer.seek(0)
        fname = f"clientes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return send_file(buffer, as_attachment=True, download_name=fname, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except ImportError:
        flash("Necesitás instalar pandas para exportar.", "error")
        return redirect(url_for("clientes"))

# ----------- TURNOS (pantalla) -----------

@app.route("/turnos", methods=["GET", "POST"])
def turnos():
    init_db()
    if request.method == "POST":
        # Alta de turno
        raw_cliente = (request.form.get("cliente_id") or "").strip()
        cliente_id = int(raw_cliente) if raw_cliente.isdigit() else None
        fecha = (request.form.get("fecha") or "").strip()    # "YYYY-MM-DD"
        hora  = (request.form.get("hora") or "").strip()     # "HH:MM"
        motivo = (request.form.get("motivo") or "").strip()

        try:
            if not (fecha and hora and motivo):
                raise ValueError("Completá fecha, hora y motivo.")
            repo_turnos_add(cliente_id, fecha, hora, motivo)
            flash("Turno creado.", "success")
            return redirect(url_for("turnos"))
        except ValueError as e:
            flash(str(e), "error")

    # Listados
    clientes = repo_list(None)
    proximos = repo_turnos_list(True)
    pasados  = repo_turnos_list(False)[:20]  # últimos 20
    return render_template("turnos.html", clientes=clientes, proximos=proximos, pasados=pasados)

@app.route("/turnos/<int:turno_id>/eliminar", methods=["POST"])
def turnos_eliminar(turno_id: int):
    init_db()
    if repo_turnos_delete(turno_id):
        flash("Turno eliminado.", "success")
    else:
        flash("No se encontró el turno.", "error")
    return redirect(url_for("turnos"))

@app.route("/turnos/<int:turno_id>/editar", methods=["GET", "POST"])
def turnos_editar(turno_id: int):
    init_db()
    row = repo_turnos_get(turno_id)
    if not row:
        flash("Turno no encontrado.", "error")
        return redirect(url_for("turnos"))

    if request.method == "POST":
        raw_cliente = (request.form.get("cliente_id") or "").strip()
        cliente_id = int(raw_cliente) if raw_cliente.isdigit() else None
        fecha = (request.form.get("fecha") or "").strip()
        hora  = (request.form.get("hora") or "").strip()
        motivo = (request.form.get("motivo") or "").strip()
        try:
            if repo_turnos_update(turno_id, cliente_id=cliente_id, fecha=fecha, hora=hora, motivo=motivo):
                flash("Turno actualizado.", "success")
            else:
                flash("Sin cambios.", "error")
            return redirect(url_for("turnos"))
        except ValueError as e:
            flash(str(e), "error")

    # Pre-completar
    # row["inicio"] = "YYYY-MM-DD HH:MM"
    fecha_pref = row["inicio"][:10]
    hora_pref  = row["inicio"][11:16]
    clientes = repo_list(None)
    return render_template(
        "turnos.html",
        clientes=clientes,
        proximos=repo_turnos_list(True),
        pasados=repo_turnos_list(False)[:20],
        edit_row=row,
        fecha_pref=fecha_pref,
        hora_pref=hora_pref,
    )

@APP.command("web")
def cli_web(
    host: str = typer.Option("127.0.0.1", help="Host"),
    port: int = typer.Option(8000, help="Puerto"),
    debug: bool = typer.Option(False, help="Debug Flask"),
):
    init_db()
    ensure_templates()  # ahora NO sobrescribe, solo crea si faltan
    from pathlib import Path
    tpl = (Path(app.root_path) / (app.template_folder or "templates")).resolve()
    console.print(f"[bold green]Servidor web en[/bold green] http://{host}:{port}")
    console.print(f"Templates en: {tpl}")
    app.run(host=host, port=port, debug=debug)

# =========================
# Entry point
# =========================
if __name__ == "__main__":
    try:
        APP()
    except Exception as e:
        console.print(f"[bold red]Error inesperado:[/bold red] {e}")
        log(f"ERROR {e}")