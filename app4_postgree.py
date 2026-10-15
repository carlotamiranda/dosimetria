# ==============================================================================
# 1. LIBRERÍAS E IMPORTACIONES
# ==============================================================================
import io
import os
import re
import pdfplumber
import numpy as np
import pandas as pd
import streamlit as st
import sqlite3

try:
    import psycopg2
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

# ==============================================================================
# 2. FUNCIONES DE BASE DE DATOS Y CÁLCULOS DOSIMÉTRICOS
# ==============================================================================

def get_db_connection():
    """
    Conecta a PostgreSQL si existe la variable DATABASE_URL.
    Si estamos en local sin secrets.toml, usa la BD SQLite local sin lanzar errores.
    """
    db_url = os.getenv("DATABASE_URL")
    
    if not db_url:
        try:
            db_url = st.secrets.get("DATABASE_URL", None)
        except Exception:
            db_url = None

    if db_url and HAS_PSYCOPG2:
        try:
            conn = psycopg2.connect(db_url, connect_timeout=10)
            conn.autocommit = True  # <--- ESTA LÍNEA ES LA SALVACIÓN
            return conn, "postgresql"
        except Exception:
            pass

    ruta_sqlite = 'C:/Users/ACPRO-PC/Desktop/dosimetria ICS/dosimetria.db'
    if not os.path.exists(ruta_sqlite):
        ruta_sqlite = 'dosimetria.db'
        
    conn = sqlite3.connect(ruta_sqlite)
    return conn, "sqlite"


def query_db(conn, engine, query, params=()):
    """ Adapta los marcadores de posición según el motor (SQLite ? vs PostgreSQL %s) """
    q = query
    if engine == "postgresql":
        q = q.replace("?", "%s")
    cursor = conn.cursor()
    cursor.execute(q, params)
    return cursor


def read_sql_db(query, conn, engine, params=None):
    """ Adapta pd.read_sql para ambos motores y evita bloqueos """
    q = query
    if engine == "postgresql":
        q = q.replace("?", "%s")
        
    try:
        return pd.read_sql(q, conn, params=params)
    except Exception as e:
        # Si falla en Postgres, forzamos un rollback para desbloquear la base de datos
        if engine == "postgresql" and hasattr(conn, 'rollback'):
            try:
                conn.rollback()
            except:
                pass
        raise e


def calcular_dosis_trabajador(lista_dosis):
    if not lista_dosis or len(lista_dosis) == 0:
        return 0.00

    dosis_limpias = []
    for val in lista_dosis:
        if isinstance(val, str):
            val_str = val.strip().upper()
            if val_str in ['FONDO', 'F', 'M'] or '<' in val_str or val_str == '':
                dosis_limpias.append(0.0)
            else:
                try:
                    dosis_limpias.append(float(val_str.replace(',', '.')))
                except ValueError:
                    dosis_limpias.append(0.0)
        elif val is None:
            dosis_limpias.append(0.0)
        else:
            dosis_limpias.append(float(val))

    dosis_array = np.array(dosis_limpias, dtype=float)
    N = len(dosis_array)

    if np.all(dosis_array < 0.10):
        return 0.00

    if N == 1:
        return round(float(dosis_array[0]), 2)

    media = np.mean(dosis_array)
    desviacion = np.std(dosis_array, ddof=1)
    return round(float(media + (2 * desviacion)), 2)


def calcular_dosis_asignada_pdf(valor_texto: str) -> float:
    if not valor_texto:
        return 0.0
    valor_limpio = str(valor_texto).strip().upper()
    if any(k in valor_limpio for k in ["FONDO", "-", "M", "F", "<"]):
        return 0.0
    try:
        return float(valor_limpio.replace(',', '.'))
    except ValueError:
        return 0.0


def extraer_lecturas_pdf_integrado(pdf_source) -> list:
    registros = []
    patron_instalacion = re.compile(r"\b([0-9]{2}[A-Z][0-9A-Z])\b")
    patron_periodo = re.compile(
        r"(?:año|mes|periodo)?\s*(20\d{2}|ENERO|FEBRERO|MARZO|ABRIL|MAYO|JUNIO|JULIO|AGOSTO|SEPTIEMBRE|OCTUBRE|NOVIEMBRE|DICIEMBRE)", 
        re.IGNORECASE
    )

    with pdfplumber.open(pdf_source) as pdf:
        num_instalacion = None
        periodo_detectado = "Anual 2026"
        mes_detectado = "Enero"
        anio_detectado = 2026

        for pagina in pdf.pages:
            texto = pagina.extract_text() or ""
            if not num_instalacion:
                match_inst = patron_instalacion.search(texto)
                if match_inst:
                    num_instalacion = match_inst.group(1)

            match_per = patron_periodo.search(texto)
            if match_per:
                val_per = match_per.group(0).strip()
                periodo_detectado = val_per.capitalize()
                match_anio = re.search(r"\b(20\d{2})\b", val_per)
                if match_anio:
                    anio_detectado = int(match_anio.group(1))
                match_mes = re.search(r"(ENERO|FEBRERO|MARZO|ABRIL|MAYO|JUNIO|JULIO|AGOSTO|SEPTIEMBRE|OCTUBRE|NOVIEMBRE|DICIEMBRE)", val_per, re.IGNORECASE)
                if match_mes:
                    mes_detectado = match_mes.group(1).capitalize()

        seccion_actual = "Personal"

        for pagina in pdf.pages:
            texto_p = pagina.extract_text() or ""
            for linea in texto_p.split("\n"):
                linea_clean = linea.strip()
                if not linea_clean:
                    continue

                linea_upper = linea_clean.upper()
                if any(k in linea_upper for k in ["SERVICIO NO PERSONAL", "ROTATORIO", "AREA RADIOLOGÍA", "CONTROL DE AREA", "CONTROL AREA"]):
                    seccion_actual = "Rotatorio / Área"
                elif any(k in linea_upper for k in ["01- RADIOLOGÍA", "PERSONAL DE ADMINISTRACIÓN", "T.S.I.D."]):
                    seccion_actual = "Personal"

                match_historia = re.match(r"^(\d{3,4})\s+(.*?)\s+(\d{2}/\d{4})\s*(.*)$", linea_clean)
                if match_historia:
                    n_historia = match_historia.group(1)
                    nombre = match_historia.group(2)
                    resto_dosis = match_historia.group(4)
                    dosis_encontradas = re.findall(r"([\d.,]+|FONDO|M|<0[.,]10|---+)", resto_dosis, re.IGNORECASE)

                    if len(dosis_encontradas) >= 2:
                        hp10_m = dosis_encontradas[0]
                        if seccion_actual == "Rotatorio / Área" or "CONTROL" in nombre.upper() or "ROTATORIO" in nombre.upper():
                            tipo_registro = "Rotatorio / Área"
                            hp007_m = dosis_encontradas[2] if len(dosis_encontradas) >= 3 else dosis_encontradas[1]
                        else:
                            tipo_registro = "Personal"
                            hp007_m = dosis_encontradas[3] if len(dosis_encontradas) >= 4 else dosis_encontradas[1]

                        registros.append({
                            "num_instalacion": num_instalacion or "08D4",
                            "num_usuario": n_historia,
                            "nombre_apellidos": nombre.strip(),
                            "tipo": tipo_registro,
                            "mes": mes_detectado,
                            "anio": anio_detectado,
                            "periodo": periodo_detectado,
                            "hp10_leida": calcular_dosis_asignada_pdf(hp10_m),
                            "hp007_leida": calcular_dosis_asignada_pdf(hp007_m)
                        })

    return registros


# ==============================================================================
# 3. CONFIGURACIÓN DE STREAMLIT Y CONEXIÓN
# ==============================================================================
st.set_page_config(
    page_title="Gestión de Dosimetría",
    page_icon="📋",
    layout="wide"
)

conn, DB_ENGINE = get_db_connection()

# ==========================================
# GESTIÓN DE SESIÓN Y LOGIN
# ==========================================
if 'usuario_logueado' not in st.session_state:
    st.session_state['usuario_logueado'] = None
    st.session_state['rol'] = None
    st.session_state['id_area'] = None
    st.session_state['nombre_area'] = None
    st.session_state['id_centro'] = None

def login():
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("### Control de Acceso")
        with st.form("form_login"):
            username = st.text_input("Usuario")
            password = st.text_input("Contraseña", type="password")
            btn_login = st.form_submit_button("Iniciar Sesión", use_container_width=True)
            
            if btn_login:
                query_login = """
                    SELECT u.username, u.rol, u.id_area_sanitaria, u.id_centro, a.nombre 
                    FROM usuarios u
                    LEFT JOIN areas_sanitarias a ON u.id_area_sanitaria = a.id_area
                    WHERE u.username = ? AND u.password = ?
                """
                cursor = query_db(conn, DB_ENGINE, query_login, (username, password))
                user = cursor.fetchone()
                
                if user:
                    st.session_state['usuario_logueado'] = user[0]
                    st.session_state['rol'] = user[1]
                    st.session_state['id_area'] = user[2]
                    st.session_state['id_centro'] = user[3]
                    st.session_state['nombre_area'] = user[4] if user[4] else "Todas / No asignada"
                    st.success(f"Bienvenido/a, {user[0]}")
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos.")

def logout():
    for key in ['usuario_logueado', 'rol', 'id_area', 'nombre_area', 'id_centro']:
        st.session_state[key] = None
    st.rerun()

if st.session_state['usuario_logueado'] is None:
    login()
    st.stop()

# ==========================================
# ESTILO CSS (ENCABEZADO HOLGADO Y PESTAÑAS AZULES)
# ==========================================
st.markdown("""
    <style>
        .block-container {
            padding-top: 2.2rem !important;
            padding-bottom: 1.5rem !important;
        }
        .header-card {
            background-color: #ffffff;
            border: 1px solid #cbd5e1;
            border-left: 6px solid #1E3A8A;
            border-radius: 8px;
            padding: 12px 18px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }
        .header-title {
            color: #1E3A8A;
            font-size: 1.5rem;
            font-weight: 700;
            margin: 0;
            padding: 0;
        }
        .header-sub {
            color: #64748B;
            font-size: 0.88rem;
            margin-top: 2px;
        }
        .user-info-text {
            font-size: 0.9rem;
            color: #1e293b;
            line-height: 1.45;
        }
        
        /* Neutralizar estilos rojos nativos en pestañas */
        button[data-baseweb="tab"] {
            color: #475569 !important;
        }
        button[data-baseweb="tab"] p {
            color: #475569 !important;
            font-size: 0.95rem !important;
        }
        button[data-baseweb="tab"][aria-selected="true"] {
            color: #1E3A8A !important;
            border-bottom-color: #1E3A8A !important;
        }
        button[data-baseweb="tab"][aria-selected="true"] p {
            color: #1E3A8A !important;
            font-weight: 700 !important;
        }
        div[data-baseweb="tab-highlight"] {
            background-color: #1E3A8A !important;
        }
    </style>
""", unsafe_allow_html=True)

def mostrar_encabezado():
    usuario = st.session_state.get('usuario_logueado', 'Usuario')
    rol = st.session_state.get('rol', 'Sin Rol')
    area = st.session_state.get('nombre_area', 'Todas / No asignada')
    
    col_tit, col_info = st.columns([2, 3])
    
    with col_tit:
        st.markdown(
            """
            <div>
                <div class="header-title">Gestión de Dosimetría</div>
                <div class="header-sub">Sistema de Control y Seguimiento Dosimétrico</div>
            </div>
            """, 
            unsafe_allow_html=True
        )
        
    with col_info:
        col_txt, col_btn = st.columns([3, 1])
        with col_txt:
            st.markdown(
                f"""
                <div class="header-card">
                    <div class="user-info-text">
                        <b>📍 Área Sanitaria:</b> {area}<br>
                        <b>👤 Usuario:</b> {usuario} <span style="color: #64748B;">({rol})</span>
                    </div>
                </div>
                """, 
                unsafe_allow_html=True
            )
        with col_btn:
            st.write("")
            if st.button("🚪 Salir", key="btn_logout_top", use_container_width=True):
                logout()

mostrar_encabezado()

# ==========================================
# CONSULTA BASE DE CENTROS SEGÚN ROL
# ==========================================
rol = st.session_state.get('rol', '')
id_area_user = st.session_state.get('id_area', None)
id_centro_user = st.session_state.get('id_centro', None)

df_centros = pd.DataFrame()

try:
    if rol == 'Admin':
        query_centros = """
            SELECT c.id_centro, c.nombre as centro, c.n_instalacion_cnd, a.nombre as area_sanitaria, a.id_area
            FROM centros c
            LEFT JOIN areas_sanitarias a ON c.id_area_sanitaria = a.id_area
        """
        df_centros = read_sql_db(query_centros, conn, DB_ENGINE)

    elif rol == 'Coordinador':
        query_centros = """
            SELECT c.id_centro, c.nombre as centro, c.n_instalacion_cnd, a.nombre as area_sanitaria, a.id_area
            FROM centros c
            JOIN areas_sanitarias a ON c.id_area_sanitaria = a.id_area
            WHERE a.id_area = ?
        """
        df_centros = read_sql_db(query_centros, conn, DB_ENGINE, params=(id_area_user,))

    elif rol == 'Centro':
        query_centros = """
            SELECT c.id_centro, c.nombre as centro, c.n_instalacion_cnd, a.nombre as area_sanitaria, a.id_area
            FROM centros c
            JOIN areas_sanitarias a ON c.id_area_sanitaria = a.id_area
            WHERE c.id_centro = ?
        """
        df_centros = read_sql_db(query_centros, conn, DB_ENGINE, params=(id_centro_user,))
        
    else:
        query_centros = """
            SELECT c.id_centro, c.nombre as centro, c.n_instalacion_cnd, a.nombre as area_sanitaria, a.id_area
            FROM centros c
            LEFT JOIN areas_sanitarias a ON c.id_area_sanitaria = a.id_area
        """
        df_centros = read_sql_db(query_centros, conn, DB_ENGINE)

except Exception as e:
    st.error(f"Error al cargar los centros: {e}")
    df_centros = pd.DataFrame()

# ==========================================
# FORMULARIO DE INICIALIZACIÓN (SI ESTÁ VACÍO)
# ==========================================
# Si la base de datos está completamente vacía y el usuario es Admin, mostramos un formulario de emergencia
if df_centros.empty and rol == 'Admin':
    st.warning("⚠️ La base de datos está vacía. Debes crear la primera Área Sanitaria y el primer Centro.")
    with st.form("form_inicializacion"):
        st.subheader("Inicializar Base de Datos")
        nueva_area = st.text_input("Nombre de la Primera Área Sanitaria (ej: Metropolitana Nord)")
        nuevo_centro = st.text_input("Nombre del Primer Centro Sanitario")
        num_cnd = st.text_input("Número de Instalación CND (opcional)")
        
        if st.form_submit_button("Crear Datos Iniciales"):
            if nueva_area and nuevo_centro:
                try:
                    if DB_ENGINE == 'sqlite':
                        cursor = conn.cursor()
                        cursor.execute("INSERT INTO areas_sanitarias (nombre) VALUES (?)", (nueva_area,))
                        id_area_nueva = cursor.lastrowid
                        cursor.execute("INSERT INTO centros (nombre, n_instalacion_cnd, id_area_sanitaria) VALUES (?, ?, ?)", 
                                       (nuevo_centro, num_cnd, id_area_nueva))
                        conn.commit()
                    elif DB_ENGINE == 'postgres':
                        cursor = conn.cursor()
                        cursor.execute("INSERT INTO areas_sanitarias (nombre) VALUES (%s) RETURNING id_area;", (nueva_area,))
                        id_area_nueva = cursor.fetchone()[0]
                        cursor.execute("INSERT INTO centros (nombre, n_instalacion_cnd, id_area_sanitaria) VALUES (%s, %s, %s);", 
                                       (nuevo_centro, num_cnd, id_area_nueva))
                        conn.commit()
                        cursor.close()
                    
                    # 👇 --- LAS LÍNEAS MÁGICAS PARA BORRAR LA MEMORIA --- 👇
                    st.cache_data.clear()
                    st.cache_resource.clear()
                    
                    st.success("¡Datos inicializados correctamente! Recargando...")
                    import time
                    time.sleep(1) # Pequeña pausa para que veas el mensaje verde
                    st.rerun()
                except Exception as e:
                    st.error(f"Error al insertar los datos iniciales: {e}")
            else:
                st.error("Debes rellenar al menos el nombre del Área y del Centro.")
    
    # Detenemos la ejecución del resto de la página hasta que se inicialice
    st.stop()


# ==========================================
# FILTRO GLOBAL DE ÁREA Y CENTRO (ÚNICO Y ARRIBA)
# ==========================================
st.markdown("---")
col_area_filtro, col_centro_filtro = st.columns(2)

with col_area_filtro:
    if rol == 'Admin':
        lista_areas = ["Todas"] + sorted(df_centros['area_sanitaria'].dropna().unique().tolist())
        area_seleccionada = st.selectbox("📍 Área Sanitaria", options=lista_areas, key="global_area_filtro")
    else:
        area_usuario = st.session_state.get('nombre_area', 'Área Sanitaria')
        area_seleccionada = area_usuario
        st.text_input("📍 Área Sanitaria", value=area_usuario, disabled=True, key="txt_area_fija")

with col_centro_filtro:
    if area_seleccionada != "Todas" and not df_centros.empty:
        df_centros_filtrados = df_centros[df_centros['area_sanitaria'] == area_seleccionada]
    else:
        df_centros_filtrados = df_centros.copy()

    if not df_centros_filtrados.empty:
        opciones_centros = sorted(df_centros_filtrados['centro'].dropna().unique().tolist())
        centro_seleccionado = st.selectbox("🏥 Centro Sanitario", options=opciones_centros, key="global_centro_filtro")
    else:
        centro_seleccionado = None
        st.warning("No hay centros disponibles para esta selección.")

id_centro_activo = None
if centro_seleccionado and not df_centros.empty:
    id_centro_activo = int(df_centros.loc[df_centros['centro'] == centro_seleccionado, 'id_centro'].values[0])

st.divider()

# ==========================================
# DEFINICIÓN DE PESTAÑAS
# ==========================================
if rol in ['Admin', 'Coordinador']:
    tab_listado, tab_gestion, tab_centros, tab_lecturas = st.tabs([
        "📋 Listado del Centro", 
        "⚙️ Altas y Bajas", 
        "🏢 Gestión de Centros", 
        "📄 Lecturas dosimétricas"
    ])
else:
    tab_listado, tab_gestion, tab_lecturas = st.tabs([
        "📋 Listado del Centro", 
        "⚙️ Altas y Bajas", 
        "📄 Lecturas dosimétricas"
    ])

# ==============================================================================
# PESTAÑA 1: LISTADO Y DOSIS ASIGNADA
# ==============================================================================
with tab_listado:
    st.header("📋 Personal y Dosimetría de Área")
    st.info(f"📍 **Centro Activo:** {centro_seleccionado}")

    if id_centro_activo:
        try:
            query_total_dosi = "SELECT COUNT(*) FROM dosimetros WHERE id_centro = ? AND activo = 1"
            cursor = query_db(conn, DB_ENGINE, query_total_dosi, (id_centro_activo,))
            total_dosimetros_centro = cursor.fetchone()[0]
        except Exception:
            total_dosimetros_centro = 0

        query_pestaña_1_dosis = """
            SELECT 
                t.id_trabajador AS id_trab,
                t.nombre AS nombre_trabajador,
                COALESCE(CAST(t.fecha_alta AS VARCHAR), 'No asignada') AS fecha_alta,
                COALESCE(CAST(t.fecha_baja AS VARCHAR), '-') AS fecha_baja,
                d.id_dosimetro,
                COALESCE(d.codigo_serie || ' (' || COALESCE(d.ubicacion, 'Sin ub.') || ')', 'Sin dosímetro') AS dosimetro_info,
                COALESCE(SUM(l.hp10_asignada), 0.0) AS hp10_dosi,
                COALESCE(SUM(l.hp007_asignada), 0.0) AS hp007_dosi
            FROM trabajadores t
            LEFT JOIN asignaciones_dosimetros ad 
                ON t.id_trabajador = ad.id_trabajador AND ad.activo = 1
            LEFT JOIN dosimetros d 
                ON ad.id_dosimetro = d.id_dosimetro AND d.activo = 1
            LEFT JOIN lecturas l 
                ON d.n_usuario_cnd = l.n_usuario_cnd
               AND d.n_instalacion_cnd = l.n_instalacion_cnd
            WHERE t.id_centro = ? AND t.activo = ?
            GROUP BY t.id_trabajador, t.nombre, t.fecha_alta, t.fecha_baja, d.id_dosimetro, d.codigo_serie, d.ubicacion
            ORDER BY t.nombre ASC
        """

        try:
            df_raw_activos = read_sql_db(query_pestaña_1_dosis, conn, DB_ENGINE, params=(id_centro_activo, 1))

            if not df_raw_activos.empty:
                filas_procesadas = []
                for id_trab, group in df_raw_activos.groupby('id_trab'):
                    nombre = group['nombre_trabajador'].iloc[0]
                    fecha_alta = group['fecha_alta'].iloc[0]
                    fecha_baja = group['fecha_baja'].iloc[0]
                    
                    dosimetros_list = [d for d in group['dosimetro_info'].tolist() if d != 'Sin dosímetro']
                    dosimetros_txt = ", ".join(dosimetros_list) if dosimetros_list else "Sin dosímetro asignado"
                    
                    lista_hp10 = group['hp10_dosi'].tolist()
                    lista_hp007 = group['hp007_dosi'].tolist()

                    dosis_hp10_calc = calcular_dosis_trabajador(lista_hp10)
                    dosis_hp007_calc = calcular_dosis_trabajador(lista_hp007)

                    filas_procesadas.append({
                        "ID Trab.": id_trab,
                        "Nombre Trabajador": nombre,
                        "Fecha Alta": fecha_alta,
                        "Fecha Baja": fecha_baja,
                        "Dosímetros Asignados": dosimetros_txt,
                        "Nº Áreas (N)": len(dosimetros_list),
                        "Dosis Asignada Hp(10) (mSv)": dosis_hp10_calc,
                        "Dosis Asignada Hp(0,07) (mSv)": dosis_hp007_calc
                    })

                df_pestaña1_activos = pd.DataFrame(filas_procesadas)

                col_m1, col_m2, col_m3, col_m4 = st.columns(4)
                with col_m1:
                    st.metric("Personal Activo", len(df_pestaña1_activos))
                with col_m2:
                    con_dosi = (df_pestaña1_activos['Dosímetros Asignados'] != 'Sin dosímetro asignado').sum()
                    st.metric("Personal con Dosímetro", con_dosi)
                with col_m3:
                    st.metric("Total Dosímetros Centro", total_dosimetros_centro)
                with col_m4:
                    max_dosis = df_pestaña1_activos['Dosis Asignada Hp(10) (mSv)'].max()
                    st.metric("Dosis Máx. Hp(10)", f"{max_dosis} mSv")

                st.divider()
                st.subheader("Personal en Alta y Dosis Asignada")
                st.dataframe(df_pestaña1_activos, use_container_width=True, hide_index=True)
            else:
                st.metric("Total Dosímetros del Centro", total_dosimetros_centro)
                st.info(f"No hay personal activo registrado en {centro_seleccionado}.")

            df_raw_bajas = read_sql_db(query_pestaña_1_dosis, conn, DB_ENGINE, params=(id_centro_activo, 0))
            if not df_raw_bajas.empty:
                filas_bajas = []
                for id_trab, group in df_raw_bajas.groupby('id_trab'):
                    dosimetros_list = [d for d in group['dosimetro_info'].tolist() if d != 'Sin dosímetro']
                    filas_bajas.append({
                        "ID Trab.": id_trab,
                        "Nombre Trabajador": group['nombre_trabajador'].iloc[0],
                        "Fecha Alta": group['fecha_alta'].iloc[0],
                        "Fecha Baja": group['fecha_baja'].iloc[0],
                        "Dosímetros Asignados": ", ".join(dosimetros_list) if dosimetros_list else "Sin dosímetro",
                        "Dosis Asignada Hp(10) (mSv)": calcular_dosis_trabajador(group['hp10_dosi'].tolist()),
                        "Dosis Asignada Hp(0,07) (mSv)": calcular_dosis_trabajador(group['hp007_dosi'].tolist())
                    })
                st.write("")
                with st.expander(f"📋 Ver Personal en Baja / Inactivo ({len(filas_bajas)} registros)"):
                    st.dataframe(pd.DataFrame(filas_bajas), use_container_width=True, hide_index=True)

        except Exception as e:
            st.error(f"Error al cargar el listado: {e}")

# ==============================================================================
# PESTAÑA 2: GESTIÓN (ALTAS, BAJAS Y ASIGNACIONES MÚLTIPLES)
# ==============================================================================
with tab_gestion:
    st.header("⚙️ Gestión de Personal, Dosímetros y Asignaciones")
    st.info(f"📍 **Centro Activo:** {centro_seleccionado}")

    if id_centro_activo:
        # ----------------------------------------------------------------------
        # 1. GESTIÓN DE PERSONAL
        # ----------------------------------------------------------------------
        st.subheader("👥 Gestión de Personal")
        col_alta_trab, col_baja_trab = st.columns(2)

        with col_alta_trab:
            st.markdown("##### ➕ Alta de Trabajador")
            with st.form("alta_trabajador_form", clear_on_submit=True):
                nombre_trab = st.text_input("Nombre y Apellidos del Trabajador")
                dni_trab = st.text_input("DNI / NIE")
                fecha_alta_trab = st.date_input("Fecha de Alta", value=pd.to_datetime("today"))
                
                if st.form_submit_button("Registrar Trabajador"):
                    if nombre_trab.strip():
                        try:
                            q_insert_trab = "INSERT INTO trabajadores (nombre, dni, id_centro, fecha_alta, activo) VALUES (?, ?, ?, ?, 1)"
                            query_db(conn, DB_ENGINE, q_insert_trab, (nombre_trab.strip(), dni_trab.strip(), id_centro_activo, str(fecha_alta_trab)))
                            conn.commit()
                            st.success(f"✔ Trabajador '{nombre_trab.strip()}' registrado.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error al registrar trabajador: {e}")
                    else:
                        st.warning("El nombre es obligatorio.")

        with col_baja_trab:
            st.markdown("##### ➖ Dar de Baja Trabajador")
            try:
                query_trab_activos = "SELECT t.id_trabajador, t.nombre FROM trabajadores t WHERE t.id_centro = ? AND t.activo = 1 ORDER BY t.nombre ASC"
                df_trab_activos = read_sql_db(query_trab_activos, conn, DB_ENGINE, params=(id_centro_activo,))

                if not df_trab_activos.empty:
                    with st.form("baja_trabajador_form"):
                        df_trab_activos['selector'] = df_trab_activos['id_trabajador'].astype(str) + " - " + df_trab_activos['nombre']
                        trab_a_baja = st.selectbox("Seleccionar Trabajador:", df_trab_activos['selector'])
                        fecha_baja_trab = st.date_input("Fecha de Baja", value=pd.to_datetime("today"))

                        if st.form_submit_button("Dar de Baja"):
                            id_trab_baja = int(trab_a_baja.split(" - ")[0])
                            query_db(conn, DB_ENGINE, "UPDATE trabajadores SET activo = 0, fecha_baja = ? WHERE id_trabajador = ?", (str(fecha_baja_trab), id_trab_baja))
                            query_db(conn, DB_ENGINE, "UPDATE asignaciones_dosimetros SET activo = 0 WHERE id_trabajador = ?", (id_trab_baja,))
                            conn.commit()
                            st.success("✔ Trabajador dado de baja correctamente.")
                            st.rerun()
                else:
                    st.info(f"No hay trabajadores activos en {centro_seleccionado}.")
            except Exception as e:
                st.error(f"Error en trabajadores: {e}")

        st.divider()

        # ----------------------------------------------------------------------
        # 2. GESTIÓN DE DOSÍMETROS
        # ----------------------------------------------------------------------
        st.subheader("📟 Gestión de Dosímetros de Área")
        col_alta_dosi, col_baja_dosi = st.columns(2)

        with col_alta_dosi:
            st.markdown("##### ➕ Alta de Dosímetro")
            with st.form("alta_dosimetro_form", clear_on_submit=True):
                codigo_serie = st.text_input("Código / Serie Dosímetro")
                ubicacion = st.text_input("Ubicación (Ej: Sala Rayos 1)")
                n_usuario_cnd = st.text_input("Nº Usuario CND")
                n_instalacion_cnd = st.text_input("Nº Instalación CND")

                if st.form_submit_button("Añadir Dosímetro"):
                    if codigo_serie.strip():
                        try:
                            q_ins_dosi = "INSERT INTO dosimetros (id_centro, codigo_serie, ubicacion, n_usuario_cnd, n_instalacion_cnd, activo) VALUES (?, ?, ?, ?, ?, 1)"
                            query_db(conn, DB_ENGINE, q_ins_dosi, (id_centro_activo, codigo_serie.strip(), ubicacion.strip(), n_usuario_cnd.strip(), n_instalacion_cnd.strip()))
                            conn.commit()
                            st.success("✔ Dosímetro registrado correctamente.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error al guardar dosímetro: {e}")
                    else:
                        st.warning("El código de serie es obligatorio.")

        with col_baja_dosi:
            st.markdown("##### ➖ Retirar Dosímetro Activo")
            try:
                query_dosi_activos = "SELECT d.id_dosimetro, d.codigo_serie, d.ubicacion FROM dosimetros d WHERE d.id_centro = ? AND d.activo = 1"
                df_dosi_activos = read_sql_db(query_dosi_activos, conn, DB_ENGINE, params=(id_centro_activo,))

                if not df_dosi_activos.empty:
                    with st.form("baja_dosimetro_form"):
                        df_dosi_activos['selector'] = df_dosi_activos['id_dosimetro'].astype(str) + " - " + df_dosi_activos['codigo_serie'] + " (" + df_dosi_activos['ubicacion'].fillna('-') + ")"
                        dosi_a_baja = st.selectbox("Seleccionar Dosímetro:", df_dosi_activos['selector'])

                        if st.form_submit_button("Retirar Dosímetro"):
                            id_dosi_baja = int(dosi_a_baja.split(" - ")[0])
                            query_db(conn, DB_ENGINE, "UPDATE dosimetros SET activo = 0 WHERE id_dosimetro = ?", (id_dosi_baja,))
                            query_db(conn, DB_ENGINE, "UPDATE asignaciones_dosimetros SET activo = 0 WHERE id_dosimetro = ?", (id_dosi_baja,))
                            conn.commit()
                            st.success("✔ Dosímetro retirado.")
                            st.rerun()
                else:
                    st.info(f"No hay dosímetros activos registrados en {centro_seleccionado}.")
            except Exception as e:
                st.error(f"Error en dosímetros: {e}")

        st.divider()

        # ----------------------------------------------------------------------
        # 3. ASIGNACIONES MÚLTIPLES (TRABAJADORES ↔ DOSÍMETROS)
        # ----------------------------------------------------------------------
        st.subheader("🔗 Asignación Múltiple (Trabajadores ↔ Dosímetros)")
        col_nueva_asig, col_borra_asig = st.columns(2)

        with col_nueva_asig:
            st.markdown("##### ➕ Vincular Trabajador y Dosímetro")
            try:
                q_t = "SELECT id_trabajador, nombre FROM trabajadores WHERE id_centro = ? AND activo = 1 ORDER BY nombre ASC"
                df_t_asig = read_sql_db(q_t, conn, DB_ENGINE, params=(id_centro_activo,))

                q_d = "SELECT id_dosimetro, codigo_serie, ubicacion FROM dosimetros WHERE id_centro = ? AND activo = 1 ORDER BY codigo_serie ASC"
                df_d_asig = read_sql_db(q_d, conn, DB_ENGINE, params=(id_centro_activo,))

                if not df_t_asig.empty and not df_d_asig.empty:
                    with st.form("form_nueva_asignacion", clear_on_submit=True):
                        df_t_asig['selector'] = df_t_asig['id_trabajador'].astype(str) + " - " + df_t_asig['nombre']
                        df_d_asig['selector'] = df_d_asig['id_dosimetro'].astype(str) + " - " + df_d_asig['codigo_serie'] + " (" + df_d_asig['ubicacion'].fillna('-') + ")"

                        trab_sel = st.selectbox("Seleccionar Trabajador:", df_t_asig['selector'])
                        dosi_sel = st.selectbox("Seleccionar Dosímetro:", df_d_asig['selector'])
                        fecha_asig = st.date_input("Fecha Asignación", value=pd.to_datetime("today"))

                        if st.form_submit_button("Crear Asignación"):
                            id_trab = int(trab_sel.split(" - ")[0])
                            id_dosi = int(dosi_sel.split(" - ")[0])

                            q_check = "SELECT id_asignacion FROM asignaciones_dosimetros WHERE id_trabajador = ? AND id_dosimetro = ? AND activo = 1"
                            cur_check = query_db(conn, DB_ENGINE, q_check, (id_trab, id_dosi))
                            if cur_check.fetchone():
                                st.warning("⚠️ Esta asignación ya se encuentra activa.")
                            else:
                                q_ins_asig = "INSERT INTO asignaciones_dosimetros (id_trabajador, id_dosimetro, fecha_asignacion, activo) VALUES (?, ?, ?, 1)"
                                query_db(conn, DB_ENGINE, q_ins_asig, (id_trab, id_dosi, str(fecha_asig)))
                                conn.commit()
                                st.success("✔ Asignación registrada correctamente.")
                                st.rerun()
                else:
                    st.info(f"Se requiere al menos 1 trabajador y 1 dosímetro activo en {centro_seleccionado} para crear asignaciones.")
            except Exception as e:
                st.error(f"Error al cargar opciones de vinculación: {e}")

        with col_borra_asig:
            st.markdown("##### ➖ Finalizar Asignación Específica")
            try:
                query_asig_activas = """
                    SELECT 
                        ad.id_asignacion,
                        t.nombre AS trabajador,
                        d.codigo_serie AS dosimetro,
                        d.ubicacion
                    FROM asignaciones_dosimetros ad
                    JOIN trabajadores t ON ad.id_trabajador = t.id_trabajador
                    JOIN dosimetros d ON ad.id_dosimetro = d.id_dosimetro
                    WHERE t.id_centro = ? AND ad.activo = 1 AND t.activo = 1
                    ORDER BY t.nombre ASC
                """
                df_asig_activas = read_sql_db(query_asig_activas, conn, DB_ENGINE, params=(id_centro_activo,))

                if not df_asig_activas.empty:
                    with st.form("form_finalizar_asig"):
                        df_asig_activas['selector'] = (
                            df_asig_activas['id_asignacion'].astype(str) + " - " + 
                            df_asig_activas['trabajador'] + " ↔ " + df_asig_activas['dosimetro'] + 
                            " (" + df_asig_activas['ubicacion'].fillna('-') + ")"
                        )
                        asig_a_fin = st.selectbox("Seleccionar Asignación a finalizar:", df_asig_activas['selector'])

                        if st.form_submit_button("Finalizar Asignación"):
                            id_asig_fin = int(asig_a_fin.split(" - ")[0])
                            query_db(conn, DB_ENGINE, "UPDATE asignaciones_dosimetros SET activo = 0 WHERE id_asignacion = ?", (id_asig_fin,))
                            conn.commit()
                            st.success(f"✔ Asignación ID {id_asig_fin} finalizada.")
                            st.rerun()
                else:
                    st.info(f"No hay asignaciones activas para finalizar en {centro_seleccionado}.")
            except Exception as e:
                st.error(f"Error al cargar asignaciones para finalizar: {e}")

        st.divider()

        # ----------------------------------------------------------------------
        # 4. RESUMEN DE ASIGNACIONES ACTIVAS
        # ----------------------------------------------------------------------
        st.subheader(f"📋 Resumen de Asignaciones Activas en {centro_seleccionado}")
        query_resumen = """
            SELECT 
                ad.id_asignacion AS 'ID Asig.',
                t.id_trabajador AS 'ID Trab.',
                t.nombre AS 'Trabajador',
                d.id_dosimetro AS 'ID Dosi.',
                d.codigo_serie AS 'Código Dosímetro',
                d.ubicacion AS 'Ubicación / Sala',
                d.n_usuario_cnd AS 'Usuario CND',
                d.n_instalacion_cnd AS 'Instalación CND',
                COALESCE(CAST(ad.fecha_asignacion AS VARCHAR), '-') AS 'Fecha Asignación'
            FROM asignaciones_dosimetros ad
            JOIN trabajadores t ON ad.id_trabajador = t.id_trabajador
            JOIN dosimetros d ON ad.id_dosimetro = d.id_dosimetro
            WHERE t.id_centro = ? AND ad.activo = 1 AND t.activo = 1
            ORDER BY t.nombre ASC, ad.id_asignacion DESC
        """
        try:
            df_resumen = read_sql_db(query_resumen, conn, DB_ENGINE, params=(id_centro_activo,))
            if not df_resumen.empty:
                st.caption(f"Mostrando {len(df_resumen)} asignaciones activas.")
                st.dataframe(df_resumen, use_container_width=True, hide_index=True)
            else:
                st.info(f"No hay asignaciones activas registradas en {centro_seleccionado}.")
        except Exception as e:
            st.error(f"Error al cargar el resumen de asignaciones: {e}")

# ==============================================================================
# PESTAÑA 3: GESTIÓN DE CENTROS
# ==============================================================================
if rol in ['Admin', 'Coordinador']:
    with tab_centros:
        st.header("🏢 Gestión y Creación de Centros")
        
        if rol == 'Admin':
            df_areas = read_sql_db("SELECT id_area, nombre FROM areas_sanitarias ORDER BY nombre ASC", conn, DB_ENGINE)
        else:
            df_areas = read_sql_db("SELECT id_area, nombre FROM areas_sanitarias WHERE id_area = ?", conn, DB_ENGINE, params=(id_area_user,))

        if not df_areas.empty:
            st.caption("Añade un centro sanitario asignándole su código de instalación CND correspondiente.")
            with st.form("nuevo_centro", clear_on_submit=True):
                col1, col2 = st.columns(2)
                with col1:
                    nombre_centro = st.text_input("Nombre del Centro")
                    area_seleccionada = st.selectbox("Área Sanitaria", df_areas['nombre'])
                with col2:
                    n_instalacion_cnd = st.text_input("Nº Instalación CND (4 digitos)")

                if st.form_submit_button("💾 Crear Centro"):
                    if nombre_centro.strip() and n_instalacion_cnd.strip():
                        try:
                            id_area = df_areas.loc[df_areas['nombre'] == area_seleccionada, 'id_area'].values[0]
                            q_ins_c = "INSERT INTO centros (nombre, id_area_sanitaria, n_instalacion_cnd) VALUES (?, ?, ?)"
                            query_db(conn, DB_ENGINE, q_ins_c, (nombre_centro.strip(), int(id_area), n_instalacion_cnd.strip().upper()))
                            conn.commit()
                            st.success(f"🎉 Centro '{nombre_centro}' creado con éxito.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error al crear centro: {e}")
                    else:
                        st.warning("⚠️ Debes rellenar todos los campos.")

# ==============================================================================
# PESTAÑA 4: LECTURAS DOSIMÉTRICAS
# ==============================================================================
with tab_lecturas:
    st.header("📄 Gestión e Importación de Lecturas Dosimétricas")
    st.info(f"📍 **Centro Activo:** {centro_seleccionado}")

    subtab_upload, subtab_ver = st.tabs(["📤 Subir Informe PDF", "📊 Consultar Histórico"])

    # --------------------------------------------------------------------------
    # SUB-PESTAÑA A: SUBIR PDF SIN DUPLICADOS
    # --------------------------------------------------------------------------
    with subtab_upload:
        uploaded_file = st.file_uploader("Selecciona el archivo PDF de lecturas:", type=["pdf"], key="pdf_uploader_cnd")
        if uploaded_file is not None:
            if st.button("⚙️ Extraer e Importar Lecturas", type="primary"):
                try:
                    lecturas_extraidas = extraer_lecturas_pdf_integrado(uploaded_file)
                    if lecturas_extraidas:
                        df_resultado = pd.DataFrame(lecturas_extraidas)
                        registros_actualizados = 0

                        for _, row in df_resultado.iterrows():
                            inst = str(row['num_instalacion']).strip()
                            usr = str(row['num_usuario']).strip()
                            mes = str(row['mes']).strip()
                            anio = int(row['anio'])
                            hp10 = float(row['hp10_leida'])
                            hp007 = float(row['hp007_leida'])

                            # 1. Eliminar si ya existe exactamente esa lectura (evita duplicados)
                            q_del = "DELETE FROM lecturas WHERE n_instalacion_cnd = ? AND n_usuario_cnd = ? AND mes = ? AND anio = ?"
                            query_db(conn, DB_ENGINE, q_del, (inst, usr, mes, anio))

                            # 2. Insertar el registro nuevo / actualizado
                            q_ins = """
                                INSERT INTO lecturas 
                                (n_instalacion_cnd, n_usuario_cnd, mes, anio, hp10_asignada, hp007_asignada)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """
                            query_db(conn, DB_ENGINE, q_ins, (inst, usr, mes, anio, hp10, hp007))
                            registros_actualizados += 1

                        conn.commit()
                        st.success(f"✔ ¡Éxito! Se guardaron/actualizaron {registros_actualizados} lecturas sin duplicados.")
                    else:
                        st.warning("No se encontraron lecturas válidas en el PDF.")
                except Exception as e:
                    st.error(f"Error al procesar el PDF: {e}")

    # --------------------------------------------------------------------------
    # SUB-PESTAÑA B: HISTÓRICO
    # --------------------------------------------------------------------------
    with subtab_ver:
        if id_centro_activo:
            try:
                n_inst_sel = df_centros.loc[df_centros["id_centro"] == id_centro_activo, "n_instalacion_cnd"].values[0]
                query_historico = """
                    SELECT n_instalacion_cnd AS "Instalación", n_usuario_cnd AS "Usuario CND", 
                           (mes || ' ' || CAST(anio AS VARCHAR)) AS "Periodo", 
                           hp10_asignada AS "Hp(10)", hp007_asignada AS "Hp(0,07)"
                    FROM lecturas
                    WHERE n_instalacion_cnd = ?
                    ORDER BY anio DESC, id DESC
                """
                df_historico = read_sql_db(query_historico, conn, DB_ENGINE, params=(str(n_inst_sel),))
                if not df_historico.empty:
                    st.dataframe(df_historico, use_container_width=True, hide_index=True)
                else:
                    st.info(f"No hay lecturas registradas para la instalación CND {n_inst_sel}.")
            except Exception as e:
                st.error(f"Error al cargar histórico: {e}")

conn.close()