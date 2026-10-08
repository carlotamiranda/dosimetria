# ==============================================================================
# 1. LIBRERÍAS E IMPORTACIONES
# ==============================================================================
import io
import re
import pdfplumber
import numpy as np
import pandas as pd
import streamlit as st
import sqlite3

# Reemplaza la línea que da error por esta comprobación segura
try:
    DATABASE_URL = os.environ.get("DATABASE_URL") or st.secrets.get("DATABASE_URL")
except Exception:
    DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    import psycopg2
    # Si la URL viene de Heroku/Render con postgres://, psycopg2 prefiere postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    
    conn = psycopg2.connect(DATABASE_URL)
    DB_ENGINE = "postgresql"
else:
    conn = sqlite3.connect("dosimetria.db", check_same_thread=False)
    DB_ENGINE = "sqlite"





# ==============================================================================
# 2. FUNCIONES AUXILIARES Y CÁLCULOS DOSIMÉTRICOS (DEFINIDAS AL PRINCIPIO)
# ==============================================================================



# ==============================================================================
# CONEXIÓN A LA BASE DE DATOS (POSTGRESQL CON FALLBACK A SQLITE)
# ==============================================================================

def get_db_connection():
    """
    Intenta conectar a PostgreSQL usando la variable DB_URL o st.secrets.
    Si no encuentra configuración de PostgreSQL, conecta a la BD local SQLite.
    """
    # 1. Intentar conexión a PostgreSQL
    db_url = os.getenv("DATABASE_URL") or st.secrets.get("DATABASE_URL", None)
    
    if db_url:
        try:
            conn = psycopg2.connect(db_url)
            return conn, "postgresql"
        except Exception as e:
            st.error(f"Error al conectar a PostgreSQL: {e}")



def calcular_dosis_trabajador(lista_dosis):
    """
    Aplica la norma de asignación según el número de áreas (N) y la fórmula de la imagen:
    - FONDO / M / <0.10 -> 0.00 mSv
    - N = 1 -> Dosis directa
    - N > 1 -> Media + 2 * Desviación Típica Muestral (ddof=1)
    """
    if not lista_dosis or len(lista_dosis) == 0:
        return 0.00

    # Limpieza y conversión
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

    # Nivel de registro (< 0.10 mSv)
    if np.all(dosis_array < 0.10):
        return 0.00

    # Caso N = 1
    if N == 1:
        return round(float(dosis_array[0]), 2)

    # Caso N > 1 (Media + 2 * Desviación típica)
    media = np.mean(dosis_array)
    desviacion = np.std(dosis_array, ddof=1)
    
    return round(float(media + (2 * desviacion)), 2)


def calcular_dosis_asignada_pdf(valor_texto: str) -> float:
    """ Convierte texto de dosis del PDF ('FONDO', 'M', '<0.10', etc.) a flotante """
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
    """
    Extrae las lecturas mensuales del PDF del CND clasificando correctamente
    si es Personal o Área / Rotatorio según la sección del informe.
    """
    registros = []
    
    # Patrón para código de instalación (ej: 08D4)
    patron_instalacion = re.compile(r"\b([0-9]{2}[A-Z][0-9A-Z])\b")
    
    # Patrón para capturar mes/año
    patron_periodo = re.compile(
        r"(?:año|mes|periodo)?\s*(20\d{2}|ENERO|FEBRERO|MARZO|ABRIL|MAYO|JUNIO|JULIO|AGOSTO|SEPTIEMBRE|OCTUBRE|NOVIEMBRE|DICIEMBRE)", 
        re.IGNORECASE
    )

    with pdfplumber.open(pdf_source) as pdf:
        num_instalacion = None
        periodo_detectado = "Anual 2025"
        mes_detectado = "Enero"
        anio_detectado = 2026

        # 1. Obtener metadatos globales
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

        # 2. Leer las filas evaluando el tipo de sección
        seccion_actual = "Personal"

        for pagina in pdf.pages:
            texto_p = pagina.extract_text() or ""
            
            for linea in texto_p.split("\n"):
                linea_clean = linea.strip()
                if not linea_clean:
                    continue

                # Identificar si entramos en sección de Áreas / Rotatorios o Personal
                linea_upper = linea_clean.upper()
                if any(k in linea_upper for k in ["SERVICIO NO PERSONAL", "ROTATORIO", "AREA RADIOLOGÍA", "CONTROL DE AREA", "CONTROL AREA"]):
                    seccion_actual = "Rotatorio / Área"
                elif any(k in linea_upper for k in ["01- RADIOLOGÍA", "PERSONAL DE ADMINISTRACIÓN", "T.S.I.D."]):
                    seccion_actual = "Personal"

                # Expresión regular para filas de datos:
                # [Historia] [Nombre] [Alta (mm/yyyy)] [Baja (opcional)] [Dosis...]
                # Captura líneas que empiezan por 3 o 4 dígitos de Historia
                match_historia = re.match(r"^(\d{3,4})\s+(.*?)\s+(\d{2}/\d{4})\s*(.*)$", linea_clean)
                
                if match_historia:
                    n_historia = match_historia.group(1)
                    nombre = match_historia.group(2)
                    resto_dosis = match_historia.group(4)

                    # Extraer todos los valores de dosis de la parte final
                    dosis_encontradas = re.findall(r"([\d.,]+|FONDO|M|<0[.,]10|---+)", resto_dosis, re.IGNORECASE)

                    if len(dosis_encontradas) >= 2:
                        # Para Área: dosis_encontradas[0] -> Profunda Mensual | dosis_encontradas[2] (o [1]) -> Superficial Mensual
                        hp10_m = dosis_encontradas[0]
                        
                        if seccion_actual == "Rotatorio / Área" or "CONTROL" in nombre.upper() or "ROTATORIO" in nombre.upper():
                            tipo_registro = "Rotatorio / Área"
                            # En Área: [Profunda Mensual, Profunda Anual, Superficial Mensual, Superficial Anual]
                            hp007_m = dosis_encontradas[2] if len(dosis_encontradas) >= 3 else dosis_encontradas[1]
                        else:
                            tipo_registro = "Personal"
                            # En Personal: [Profunda Mensual, Prof. Quinquenal, Prof. Anual, Superficial Mensual, Sup. Anual]
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
# 3. CONEXIÓN A BASE DE DATOS Y CONFIGURACIÓN DE STREAMLIT
# ==============================================================================
# Configuración básica de la página




st.set_page_config(page_title="Dosimetría - Control de Acceso", layout="wide")

def get_conexion():
    return sqlite3.connect('C:/Users/ACPRO-PC/Desktop/dosimetria ICS/dosimetria.db')

conn = get_conexion()



# ==========================================
# INICIALIZACIÓN Y MIGRACIONES DE ESQUEMA
# ==========================================
# ==========================================
# INICIALIZACIÓN Y MIGRACIONES DE ESQUEMA
# ==========================================
cursor = conn.cursor()

# 1. Tabla de registros de horas
cursor.execute("""
    CREATE TABLE IF NOT EXISTS registros_horas (
        id_registro INTEGER PRIMARY KEY AUTOINCREMENT,
        id_trabajador INTEGER,
        mes_anio TEXT,
        horas REAL,
        dosis_total REAL,
        FOREIGN KEY(id_trabajador) REFERENCES trabajadores(id_trabajador)
    );
""")

# 2. Tabla intermedia de asignaciones
cursor.execute("""
    CREATE TABLE IF NOT EXISTS asignaciones_dosimetros (
        id_asignacion INTEGER PRIMARY KEY AUTOINCREMENT,
        id_trabajador INTEGER NOT NULL,
        id_dosimetro INTEGER NOT NULL,
        fecha_asignacion TEXT DEFAULT (datetime('now', 'localtime')),
        activo INTEGER DEFAULT 1,
        FOREIGN KEY(id_trabajador) REFERENCES trabajadores(id_trabajador),
        FOREIGN KEY(id_dosimetro) REFERENCES dosimetros(id_dosimetro)
    );
""")

# 3. Asegurar columnas de fechas en trabajadores
try:
    cursor.execute("ALTER TABLE trabajadores ADD COLUMN fecha_alta TEXT;")
except Exception:
    pass

try:
    cursor.execute("ALTER TABLE trabajadores ADD COLUMN fecha_baja TEXT;")
except Exception:
    pass

conn.commit()

# ==========================================
# GESTIÓN DE SESIÓN Y LOGIN
# ==========================================
if 'usuario_logueado' not in st.session_state:
    st.session_state['usuario_logueado'] = None
    st.session_state['rol'] = None
    st.session_state['id_area'] = None
    st.session_state['id_centro'] = None

def login():
    st.title("Control de Acceso - Gestión de dosimetría")
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("form_login"):
            username = st.text_input("Usuario")
            password = st.text_input("Contraseña", type="password")
            btn_login = st.form_submit_button("Iniciar Sesión", use_container_width=True)
            
            if btn_login:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT username, rol, id_area_sanitaria, id_centro FROM usuarios WHERE username = ? AND password = ?", 
                    (username, password)
                )
                user = cursor.fetchone()
                if user:
                    st.session_state['usuario_logueado'] = user[0]
                    st.session_state['rol'] = user[1]
                    st.session_state['id_area'] = user[2]
                    st.session_state['id_centro'] = user[3]
                    st.success(f"Bienvenido/a, {user[0]}")
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos.")

def logout():
    st.session_state['usuario_logueado'] = None
    st.session_state['rol'] = None
    st.session_state['id_area'] = None
    st.session_state['id_centro'] = None
    st.rerun()

# Si no está logueado, mostramos el login y detendremos la ejecución aquí
if st.session_state['usuario_logueado'] is None:
    login()
    st.stop()

# ==========================================
# CABECERA DE USUARIO LOGUEADO
# ==========================================
col_titulo, col_user = st.columns([4, 1])
with col_titulo:
    st.title("Gestión Integral - Dosimetría de Área")
with col_user:
    st.write(f"👤 **{st.session_state['usuario_logueado']}**")
    st.caption(f"Rol: {st.session_state['rol']}")
    if st.button("Cerrar Sesión"):
        logout()

st.divider()

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
        df_centros = pd.read_sql(query_centros, conn)

    elif rol == 'Coordinador':
        # Adaptador de marcadores según motor de BD (? vs %s)
        query_centros = """
            SELECT c.id_centro, c.nombre as centro, c.n_instalacion_cnd, a.nombre as area_sanitaria, a.id_area
            FROM centros c
            JOIN areas_sanitarias a ON c.id_area_sanitaria = a.id_area
            WHERE a.id_area = ?
        """
        if DB_ENGINE == "postgresql":
            query_centros = query_centros.replace("?", "%s")
            
        df_centros = pd.read_sql(query_centros, conn, params=(id_area_user,))

    elif rol == 'Centro':
        query_centros = """
            SELECT c.id_centro, c.nombre as centro, c.n_instalacion_cnd, a.nombre as area_sanitaria, a.id_area
            FROM centros c
            JOIN areas_sanitarias a ON c.id_area_sanitaria = a.id_area
            WHERE c.id_centro = ?
        """
        if DB_ENGINE == "postgresql":
            query_centros = query_centros.replace("?", "%s")

        df_centros = pd.read_sql(query_centros, conn, params=(id_centro_user,))
        
    else:
        query_centros = """
            SELECT c.id_centro, c.nombre as centro, c.n_instalacion_cnd, a.nombre as area_sanitaria, a.id_area
            FROM centros c
            LEFT JOIN areas_sanitarias a ON c.id_area_sanitaria = a.id_area
        """
        df_centros = pd.read_sql(query_centros, conn)

except Exception as e:
    st.error(f"Error al cargar los centros: {e}")
    df_centros = pd.DataFrame()

# ==========================================
# DEFINICIÓN DE PESTAÑAS SEGÚN ROL
# ==========================================
# Permite tab_centros tanto a 'Admin' como a 'Coordinador'
if rol in ['Admin', 'Coordinador']:
    tab_listado, tab_gestion, tab_centros, tab_lecturas = st.tabs([
        "📋 Listado del Centro", 
        "⚙️ Altas y Bajas", 
        "🏥 Gestión de Centros", 
        "📄 Lecturas dosimétricas"
    ])
else:
    tab_listado, tab_gestion, tab_lecturas = st.tabs([
        "📋 Listado del Centro", 
        "⚙️ Altas y Bajas", 
        "📄 Lecturas dosimétricas"
    ])

# ==============================================================================
# PESTAÑA 1: LISTADO Y DOSIS ASIGNADA SEGÚN NORMA
# ==============================================================================
with tab_listado:
    st.header("📋 Personal y Dosimetría de Área")

    if not df_centros.empty:
        # ----------------------------------------------------------------------
        # 1. SELECTOR DE CENTRO SEGÚN EL ROL DEL USUARIO
        # ----------------------------------------------------------------------
        lista_centros_p1 = df_centros['centro'].unique()

        if len(lista_centros_p1) == 1:
            centro_sel_p1 = lista_centros_p1[0]
            st.info(f"📍 **Centro activo:** {centro_sel_p1}")
        else:
            centro_sel_p1 = st.selectbox(
                "Seleccionar Centro:", 
                lista_centros_p1, 
                key="sel_centro_p1"
            )

        # ID del centro seleccionado
        id_centro_p1 = int(df_centros.loc[df_centros['centro'] == centro_sel_p1, 'id_centro'].values[0])

        # ----------------------------------------------------------------------
        # 2. MÉTRICA: TOTAL DOSÍMETROS DEL CENTRO
        # ----------------------------------------------------------------------
        try:
            query_total_dosi = "SELECT COUNT(*) FROM dosimetros WHERE id_centro = ? AND activo = 1"
            cursor = conn.cursor()
            cursor.execute(query_total_dosi, (id_centro_p1,))
            total_dosimetros_centro = cursor.fetchone()[0]
        except Exception:
            total_dosimetros_centro = 0

        # ----------------------------------------------------------------------
        # 3. CONSULTA SQL Y CÁLCULO DE DOSIS POR TRABAJADOR
        # ----------------------------------------------------------------------
        query_pestaña_1_dosis = """
            SELECT 
                t.id_trabajador AS id_trab,
                t.nombre AS nombre_trabajador,
                COALESCE(t.fecha_alta, 'No asignada') AS fecha_alta,
                COALESCE(t.fecha_baja, '-') AS fecha_baja,
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
            # --- PERSONAL ACTIVO (t.activo = 1) ---
            df_raw_activos = pd.read_sql(query_pestaña_1_dosis, conn, params=(id_centro_p1, 1))

            if not df_raw_activos.empty:
                filas_procesadas = []

                for id_trab, group in df_raw_activos.groupby('id_trab'):
                    nombre = group['nombre_trabajador'].iloc[0]
                    fecha_alta = group['fecha_alta'].iloc[0]
                    fecha_baja = group['fecha_baja'].iloc[0]
                    
                    # Lista de dosímetros asignados
                    dosimetros_list = [d for d in group['dosimetro_info'].tolist() if d != 'Sin dosímetro']
                    dosimetros_txt = ", ".join(dosimetros_list) if dosimetros_list else "Sin dosímetro asignado"
                    
                    # Listas de dosis por cada dosímetro asignado
                    lista_hp10 = group['hp10_dosi'].tolist()
                    lista_hp007 = group['hp007_dosi'].tolist()

                    # Aplicar la función matemática (Media + 2*σ si N>1, o Dosis Directa si N=1)
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

                # --- TARJETAS DE MÉTRICAS ---
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

                # --- TABLA DE PERSONAL EN ALTA ---
                st.subheader("Personal en Alta y Dosis Asignada")
                st.dataframe(
                    df_pestaña1_activos,
                    use_container_width=True,
                    hide_index=True
                )
            else:
                st.metric("Total Dosímetros del Centro", total_dosimetros_centro)
                st.info(f"No hay personal activo registrado en {centro_sel_p1}.")

            # --- HISTÓRICO DE BAJAS (t.activo = 0) ---
            df_raw_bajas = pd.read_sql(query_pestaña_1_dosis, conn, params=(id_centro_p1, 0))
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
                    st.dataframe(
                        pd.DataFrame(filas_bajas),
                        use_container_width=True,
                        hide_index=True
                    )

        except Exception as e:
            st.error(f"Error al cargar la Pestaña 1: {e}")

    else:
        st.warning("No hay centros asignados según su rol de usuario.")

# ==========================================
# PESTAÑA 2: GESTIÓN (ALTAS, BAJAS Y ASIGNACIÓN)
# ==========================================
with tab_gestion:
    st.header("⚙️ Gestión de Personal, Dosímetros y Asignaciones Múltiples")

    if not df_centros.empty:
        # ==============================================================================
        # 1. SELECTOR DE CENTRO SEGÚN ROL
        # ==============================================================================
        lista_centros_g = df_centros['centro'].unique()
        
        if len(lista_centros_g) == 1:
            centro_sel_g = lista_centros_g[0]
            st.info(f"📍 **Centro activo:** {centro_sel_g}")
        else:
            centro_sel_g = st.selectbox(
                "Seleccionar Centro:", 
                lista_centros_g, 
                key="sel_centro_p2_gestion"
            )

        # ID exacto del centro seleccionado
        id_centro_actual = int(df_centros.loc[df_centros['centro'] == centro_sel_g, 'id_centro'].values[0])

        st.divider()

        # ==============================================================================
        # 2. SECCIÓN: GESTIÓN DE PERSONAL (TRABAJADORES)
        # ==============================================================================
        st.subheader("👥 Gestión de Personal")
        col_alta_trab, col_baja_trab = st.columns(2)

        # ------------------------------------------------------------------------------
        # 2.1 ALTA DE TRABAJADOR 
        # ------------------------------------------------------------------------------
        with col_alta_trab:
            st.markdown("##### ➕ Alta de Trabajador")
            with st.form("alta_trabajador_form", clear_on_submit=True):
                nombre_trab = st.text_input("Nombre y Apellidos del Trabajador")
                dni_trab = st.text_input("DNI / NIE")  # <--- NUEVO CAMPO DNI
                fecha_alta_trab = st.date_input("Fecha de Alta", value=pd.to_datetime("today"))
                
                if st.form_submit_button("Registrar Trabajador"):
                    if nombre_trab.strip():
                        try:
                            cursor = conn.cursor()
                            cursor.execute(
                                """
                                INSERT INTO trabajadores (nombre, dni, id_centro, fecha_alta, activo)
                                VALUES (?, ?, ?, ?, 1)
                                """,
                                (nombre_trab.strip(), dni_trab.strip(), id_centro_actual, str(fecha_alta_trab))
                            )
                            conn.commit()
                            st.success(f"✔ Trabajador '{nombre_trab.strip()}' registrado correctamente.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error al registrar trabajador: {e}")
                    else:
                        st.warning("El nombre del trabajador no puede estar vacío.")

        # ------------------------------------------------------------------------------
        # 2.2 BAJA DE TRABAJADOR
        # ------------------------------------------------------------------------------
        with col_baja_trab:
            st.markdown("##### ➖ Dar de Baja Trabajador")
            try:
                query_trab_activos = """
                    SELECT t.id_trabajador, t.nombre 
                    FROM trabajadores t
                    WHERE t.id_centro = ? AND t.activo = 1
                    ORDER BY t.nombre ASC
                """
                df_trab_activos = pd.read_sql(query_trab_activos, conn, params=(id_centro_actual,))

                if not df_trab_activos.empty:
                    with st.form("baja_trabajador_form"):
                        df_trab_activos['selector'] = (
                            df_trab_activos['id_trabajador'].astype(str) + " - " + df_trab_activos['nombre']
                        )
                        trab_a_baja = st.selectbox("Seleccionar Trabajador:", df_trab_activos['selector'])
                        fecha_baja_trab = st.date_input("Fecha de Baja", value=pd.to_datetime("today"))

                        if st.form_submit_button("Dar de Baja Trabajador"):
                            id_trab_baja = int(trab_a_baja.split(" - ")[0])
                            cursor = conn.cursor()
                            
                            # Desactivar trabajador y registrar fecha de baja
                            cursor.execute(
                                "UPDATE trabajadores SET activo = 0, fecha_baja = ? WHERE id_trabajador = ?",
                                (str(fecha_baja_trab), id_trab_baja)
                            )
                            # Desactivar TODAS las asignaciones activas de este trabajador
                            cursor.execute(
                                "UPDATE asignaciones_dosimetros SET activo = 0 WHERE id_trabajador = ?",
                                (id_trab_baja,)
                            )
                            conn.commit()
                            st.success(f"✔ Trabajador ID {id_trab_baja} dado de baja (y desactivadas sus asignaciones).")
                            st.rerun()
                else:
                    st.info(f"No hay trabajadores activos en {centro_sel_g}.")
            except Exception as e:
                st.error(f"Error al cargar la lista de trabajadores: {e}")

        st.divider()

        # ==============================================================================
        # 3. SECCIÓN: GESTIÓN DE DOSÍMETROS
        # ==============================================================================
        st.subheader("📟 Gestión de Dosímetros de Área")
        col_alta_dosi, col_baja_dosi = st.columns(2)

        # ------------------------------------------------------------------------------
        # 3.1 ALTA DE DOSÍMETRO
        # ------------------------------------------------------------------------------
        with col_alta_dosi:
            st.markdown("##### ➕ Alta de Dosímetro")
            with st.form("alta_dosimetro_form", clear_on_submit=True):
                codigo_serie = st.text_input("Código / Serie Dosímetro")
                ubicacion = st.text_input("Ubicación (Ej: Quirófano 1 / RX)")
                n_usuario_cnd = st.text_input("Nº Usuario CND")
                n_instalacion_cnd = st.text_input("Nº Instalación CND")

                if st.form_submit_button("Añadir Dosímetro"):
                    if codigo_serie.strip():
                        try:
                            cursor = conn.cursor()
                            cursor.execute(
                                """
                                INSERT INTO dosimetros (id_centro, codigo_serie, ubicacion, n_usuario_cnd, n_instalacion_cnd, activo) 
                                VALUES (?, ?, ?, ?, ?, 1)
                                """,
                                (id_centro_actual, codigo_serie.strip(), ubicacion.strip(), n_usuario_cnd.strip(), n_instalacion_cnd.strip())
                            )
                            conn.commit()
                            st.success("✔ Dosímetro registrado correctamente.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error al guardar el dosímetro: {e}")
                    else:
                        st.warning("El código de serie es obligatorio.")

        # ------------------------------------------------------------------------------
        # 3.2 BAJA DE DOSÍMETRO
        # ------------------------------------------------------------------------------
        with col_baja_dosi:
            st.markdown("##### ➖ Retirar Dosímetro Activo")
            try:
                query_dosi_activos = """
                    SELECT d.id_dosimetro, d.codigo_serie, d.ubicacion 
                    FROM dosimetros d
                    WHERE d.id_centro = ? AND d.activo = 1
                """
                df_dosi_activos = pd.read_sql(query_dosi_activos, conn, params=(id_centro_actual,))

                if not df_dosi_activos.empty:
                    with st.form("baja_dosimetro_form"):
                        df_dosi_activos['selector'] = (
                            df_dosi_activos['id_dosimetro'].astype(str) + " - " + 
                            df_dosi_activos['codigo_serie'] + " (" + df_dosi_activos['ubicacion'].fillna('Sin ub.') + ")"
                        )
                        dosi_a_baja = st.selectbox("Seleccionar Dosímetro:", df_dosi_activos['selector'])

                        if st.form_submit_button("Retirar Dosímetro"):
                            id_dosi_baja = int(dosi_a_baja.split(" - ")[0])
                            cursor = conn.cursor()
                            
                            # Desactivar dosímetro y sus asignaciones
                            cursor.execute("UPDATE dosimetros SET activo = 0 WHERE id_dosimetro = ?", (id_dosi_baja,))
                            cursor.execute("UPDATE asignaciones_dosimetros SET activo = 0 WHERE id_dosimetro = ?", (id_dosi_baja,))
                            
                            conn.commit()
                            st.success(f"✔ Dosímetro ID {id_dosi_baja} retirado.")
                            st.rerun()
                else:
                    st.info(f"No hay dosímetros activos registrados en {centro_sel_g}.")
            except Exception as e:
                st.error(f"Error al cargar la lista de dosímetros: {e}")

        st.divider()

        # ==============================================================================
        # 4. SECCIÓN: ASIGNACIONES MÚLTIPLES (MUCHOS A MUCHOS)
        # ==============================================================================
        st.subheader("🔗 Asignación Múltiple (Trabajadores ↔ Dosímetros)")
        col_nueva_asig, col_borra_asig = st.columns(2)

        # ------------------------------------------------------------------------------
        # 4.1 CREAR ASIGNACIÓN (MÚLTIPLES PERMITIDAS)
        # ------------------------------------------------------------------------------
        with col_nueva_asig:
            st.markdown("##### ➕ Vincular Trabajador y Dosímetro")
            try:
                query_trabs_centro = """
                    SELECT id_trabajador, nombre 
                    FROM trabajadores 
                    WHERE id_centro = ? AND activo = 1 
                    ORDER BY nombre ASC
                """
                df_trabs_asig = pd.read_sql(query_trabs_centro, conn, params=(id_centro_actual,))

                query_dosis_centro = """
                    SELECT id_dosimetro, codigo_serie, ubicacion 
                    FROM dosimetros 
                    WHERE id_centro = ? AND activo = 1 
                    ORDER BY codigo_serie ASC
                """
                df_dosis_asig = pd.read_sql(query_dosis_centro, conn, params=(id_centro_actual,))

                if not df_trabs_asig.empty and not df_dosis_asig.empty:
                    with st.form("form_nueva_asignacion", clear_on_submit=True):
                        df_trabs_asig['selector'] = df_trabs_asig['id_trabajador'].astype(str) + " - " + df_trabs_asig['nombre']
                        df_dosis_asig['selector'] = df_dosis_asig['id_dosimetro'].astype(str) + " - " + df_dosis_asig['codigo_serie'] + " (" + df_dosis_asig['ubicacion'].fillna('Sin ub.') + ")"

                        trab_sel = st.selectbox(f"1. Seleccionar Trabajador ({centro_sel_g}):", df_trabs_asig['selector'], key="sel_trab_asig")
                        dosi_sel = st.selectbox(f"2. Seleccionar Dosímetro ({centro_sel_g}):", df_dosis_asig['selector'], key="sel_dosi_asig")
                        fecha_asig = st.date_input("Fecha Asignación", value=pd.to_datetime("today"))

                        if st.form_submit_button("Crear Asignación"):
                            id_trab = int(trab_sel.split(" - ")[0])
                            id_dosi = int(dosi_sel.split(" - ")[0])

                            cursor = conn.cursor()
                            
                            # Comprobar si esta vinculación exacta ya está activa para no duplicar
                            cursor.execute(
                                "SELECT id_asignacion FROM asignaciones_dosimetros WHERE id_trabajador = ? AND id_dosimetro = ? AND activo = 1",
                                (id_trab, id_dosi)
                            )
                            ya_existe = cursor.fetchone()

                            if ya_existe:
                                st.warning("⚠️ Esta asignación exacta ya se encuentra activa.")
                            else:
                                # Insertar nueva asignación (permite múltiples dosímetros por trabajador)
                                cursor.execute(
                                    """
                                    INSERT INTO asignaciones_dosimetros (id_trabajador, id_dosimetro, fecha_asignacion, activo)
                                    VALUES (?, ?, ?, 1)
                                    """,
                                    (id_trab, id_dosi, str(fecha_asig))
                                )
                                conn.commit()
                                st.success("✔ Asignación registrada correctamente.")
                                st.rerun()
                else:
                    st.info(f"Se requiere al menos 1 trabajador activo y 1 dosímetro activo en {centro_sel_g} para realizar asignaciones.")
            except Exception as e:
                st.error(f"Error al cargar las opciones de asignación: {e}")

        # ------------------------------------------------------------------------------
        # 4.2 FINALIZAR UNA ASIGNACIÓN CONCRETA
        # ------------------------------------------------------------------------------
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
                df_asig_desact = pd.read_sql(query_asig_activas, conn, params=(id_centro_actual,))

                if not df_asig_desact.empty:
                    with st.form("form_desactivar_asignacion"):
                        df_asig_desact['selector'] = (
                            df_asig_desact['id_asignacion'].astype(str) + " - " + 
                            df_asig_desact['trabajador'] + " ↔ " + df_asig_desact['dosimetro'] + 
                            " (" + df_asig_desact['ubicacion'].fillna('-') + ")"
                        )
                        asig_a_borrar = st.selectbox("Seleccionar Asignación a finalizar:", df_asig_desact['selector'])

                        if st.form_submit_button("Finalizar Asignación"):
                            id_asig_fin = int(asig_a_borrar.split(" - ")[0])
                            cursor = conn.cursor()
                            cursor.execute("UPDATE asignaciones_dosimetros SET activo = 0 WHERE id_asignacion = ?", (id_asig_fin,))
                            conn.commit()
                            st.success(f"✔ Asignación ID {id_asig_fin} finalizada.")
                            st.rerun()
                else:
                    st.info(f"No hay asignaciones activas para finalizar en {centro_sel_g}.")
            except Exception as e:
                st.error(f"Error al cargar las asignaciones activas: {e}")

        st.divider()

        # ==============================================================================
        # 5. RESUMEN DE ASIGNACIONES ACTIVAS DEL CENTRO
        # ==============================================================================
        st.subheader(f"📋 Resumen de Asignaciones Activas en {centro_sel_g}")

        query_resumen_asignaciones = """
            SELECT 
                ad.id_asignacion AS 'ID Asig.',
                t.id_trabajador AS 'ID Trab.',
                t.nombre AS 'Trabajador',
                d.id_dosimetro AS 'ID Dosi.',
                d.codigo_serie AS 'Código Dosímetro',
                d.ubicacion AS 'Ubicación / Sala',
                d.n_usuario_cnd AS 'Usuario CND',
                d.n_instalacion_cnd AS 'Instalación CND',
                COALESCE(ad.fecha_asignacion, '-') AS 'Fecha Asignación'
            FROM asignaciones_dosimetros ad
            JOIN trabajadores t ON ad.id_trabajador = t.id_trabajador
            JOIN dosimetros d ON ad.id_dosimetro = d.id_dosimetro
            JOIN centros c ON t.id_centro = c.id_centro
            WHERE c.nombre = ? AND ad.activo = 1 AND t.activo = 1
            ORDER BY t.nombre ASC, ad.id_asignacion DESC
        """

        try:
            df_asig_centro = pd.read_sql(query_resumen_asignaciones, conn, params=(centro_sel_g,))

            if not df_asig_centro.empty:
                st.caption(f"Mostrando {len(df_asig_centro)} asignaciones activas (un trabajador puede tener varios dosímetros asignados).")
                st.dataframe(
                    df_asig_centro,
                    use_container_width=True,
                    hide_index=True
                )
            else:
                st.info(f"No hay asignaciones activas registradas en {centro_sel_g}.")

        except Exception as e:
            st.error(f"Error al cargar el resumen de asignaciones: {e}")

    else:
        st.warning("No hay centros asignados según su rol de usuario.")


# ==========================================
# PESTAÑA 3: GESTIÓN DE CENTROS (Admin o Gestor de Área)
# ==========================================
if rol in ['Admin', 'Coordinador']:
    with tab_centros:
        st.header("Añadir un Nuevo Centro")
        
        # 1. Cargar la lista de áreas permitidas
        if rol == 'Admin':
            query_areas = "SELECT id_area, nombre FROM areas_sanitarias ORDER BY nombre ASC"
            df_areas = pd.read_sql(query_areas, conn)
        else:
            # Coordinador: solo ve su área asignada
            query_areas = "SELECT id_area, nombre FROM areas_sanitarias WHERE id_area = ?"
            if DB_ENGINE == "postgresql":
                query_areas = query_areas.replace("?", "%s")
            df_areas = pd.read_sql(query_areas, conn, params=(id_area_user,))

        if not df_areas.empty:
            st.caption("Asigna el centro a su Área Sanitaria e indica el código de instalación asignado por el CND.")

            with st.form("nuevo_centro", clear_on_submit=True):
                col1, col2 = st.columns(2)

                with col1:
                    nombre_centro = st.text_input("Nombre del Centro (Ej: CAP Besós)")
                    
                    if len(df_areas) == 1:
                        area_seleccionada = df_areas['nombre'].iloc[0]
                        st.selectbox("Área Sanitaria", [area_seleccionada], disabled=True)
                    else:
                        area_seleccionada = st.selectbox("Área Sanitaria", df_areas['nombre'])

                with col2:
                    n_instalacion_cnd = st.text_input(
                        "Nº Instalación CND (Ej: 08B9)",
                        help="Código único de 4 caracteres que figura en el informe del CND."
                    )

                btn_crear = st.form_submit_button("💾 Crear Centro", type="primary")

                if btn_crear:
                    if not nombre_centro.strip() or not n_instalacion_cnd.strip():
                        st.warning("⚠️ Debes rellenar el Nombre del Centro y el Nº de Instalación CND.")
                    else:
                        try:
                            id_area = df_areas.loc[df_areas['nombre'] == area_seleccionada, 'id_area'].values[0]
                            cursor = conn.cursor()
                            
                            query_insert = """
                                INSERT INTO centros (nombre, id_area_sanitaria, n_instalacion_cnd) 
                                VALUES (?, ?, ?)
                            """
                            if DB_ENGINE == "postgresql":
                                query_insert = query_insert.replace("?", "%s")

                            cursor.execute(
                                query_insert, 
                                (
                                    nombre_centro.strip(), 
                                    int(id_area), 
                                    n_instalacion_cnd.strip().upper()
                                )
                            )
                            conn.commit()
                            
                            st.success(f"🎉 Centro '{nombre_centro}' (Instalación CND: `{n_instalacion_cnd.strip().upper()}`) creado en **{area_seleccionada}**.")
                            st.rerun()

                        except Exception as e:
                            if "UNIQUE constraint" in str(e) or "duplicate key" in str(e):
                                st.error(f"❌ Ya existe un centro registrado con el Nº de Instalación CND `{n_instalacion_cnd.strip().upper()}`.")
                            else:
                                st.error(f"❌ Error al crear el centro: {e}")
        else:
            st.warning("⚠️ No se encontró un Área Sanitaria asociada a tu perfil.")


# ==============================================================================
# PESTAÑA 4: LECTURAS DOSIMÉTRICAS Y CARGA PDF
# ==============================================================================
with tab_lecturas:
    st.header("📄 Gestión e Importación de Lecturas Dosimétricas")

    if not df_centros.empty:
        # 1. Selector de centro según el rol
        lista_centros_l = df_centros['centro'].unique()
        
        if len(lista_centros_l) == 1:
            centro_sel_l = lista_centros_l[0]
            st.info(f"📍 **Centro activo:** {centro_sel_l}")
        else:
            centro_sel_l = st.selectbox(
                "Seleccionar Centro:", 
                lista_centros_l, 
                key="sel_centro_p3_lecturas"
            )

        id_centro_lecturas = int(df_centros.loc[df_centros['centro'] == centro_sel_l, 'id_centro'].values[0])

        st.divider()

        subtab_upload, subtab_ver = st.tabs(["📤 Subir Informe PDF", "📊 Consultar Histórico de Lecturas"])

        # ----------------------------------------------------------------------
        # SUB-PESTAÑA A: EXTRAER E IMPORTAR LECTURAS DEL PDF
        # ----------------------------------------------------------------------
        with subtab_upload:
            st.subheader("Subir Informe Dosimétrico (PDF)")
            st.caption("Importa y almacena directamente las lecturas registradas en el informe del CND sin aplicar cálculos.")

            uploaded_file = st.file_uploader(
                "Selecciona el archivo PDF de lecturas:", 
                type=["pdf"], 
                key="pdf_uploader_cnd"
            )

            if uploaded_file is not None:
                st.success(f"📂 Archivo cargado: **{uploaded_file.name}**")
                
                if st.button("⚙️ Extraer e Importar Lecturas", type="primary"):
                    try:
                        with st.spinner("Extrayendo lecturas del informe PDF..."):
                            # 1. Extraer lecturas directamente del PDF
                            lecturas_extraidas = extraer_lecturas_pdf_integrado(uploaded_file)

                        if lecturas_extraidas:
                            df_resultado = pd.DataFrame(lecturas_extraidas)
                            
                            # Normalizar identificadores
                            df_resultado["num_usuario"] = df_resultado["num_usuario"].astype(str).str.strip()
                            df_resultado["num_instalacion"] = df_resultado["num_instalacion"].astype(str).str.strip()

                            # 2. Vista previa con los datos brutos extraídos
                            st.write(f"✔ **Se han extraído {len(df_resultado)} lecturas del archivo:**")
                            
                            df_vista = df_resultado.rename(columns={
                                "num_instalacion": "Instalación CND",
                                "num_usuario": "Usuario CND",
                                "nombre_apellidos": "Nombre / Identificador",
                                "tipo": "Tipo",
                                "mes": "Mes",
                                "anio": "Año",
                                "hp10_leida": "Hp(10) Leída (mSv)",
                                "hp007_leida": "Hp(0,07) Leída (mSv)"
                            })

                            st.dataframe(
                                df_vista[[c for c in ["Instalación CND", "Usuario CND", "Nombre / Identificador", "Tipo", "Mes", "Año", "Hp(10) Leída (mSv)", "Hp(0,07) Leída (mSv)"] if c in df_vista.columns]],
                                use_container_width=True,
                                hide_index=True
                            )

                            # 3. Guardar directamente los valores leídos en la BD
                            cursor = conn.cursor()
                            registros_insertados = 0

                            for _, row in df_resultado.iterrows():
                                cursor.execute(
                                    """
                                    INSERT OR REPLACE INTO lecturas 
                                    (n_instalacion_cnd, n_usuario_cnd, mes, anio, hp10_asignada, hp007_asignada)
                                    VALUES (?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        str(row['num_instalacion']),
                                        str(row['num_usuario']),
                                        str(row['mes']),
                                        int(row['anio']),
                                        float(row['hp10_leida']),
                                        float(row['hp007_leida'])
                                    )
                                )
                                registros_insertados += 1

                            conn.commit()
                            st.success(f"🎉 **¡Éxito!** Se han registrado {registros_insertados} lecturas tal como figuran en el informe.")
                            st.balloons()

                        else:
                            st.warning("⚠️ No se identificaron lecturas válidas en el PDF.")

                    except Exception as e:
                        st.error(f"Error al procesar el archivo PDF: {e}")

        # ======================================================================
        # SUB-PESTAÑA B: HISTÓRICO Y CONSULTA DE LECTURAS (ÚLTIMA CÁRGA VÁLIDA)
        # ======================================================================
        with subtab_ver:
            if not df_centros.empty:
                st.subheader("📊 Histórico de Lecturas Dosimétricas")

                # --------------------------------------------------------------
                # 1. FILTROS DE BÚSQUEDA
                # --------------------------------------------------------------
                col_c1, col_c2, col_c3 = st.columns([2, 1, 1])

                with col_c1:
                    centro_sel_l = st.selectbox(
                        "🏢 Seleccionar Centro:",
                        df_centros["centro"].unique(),
                        key="sb_centro_historico"
                    )

                # Obtener el n_instalacion_cnd del centro seleccionado
                n_inst_sel = df_centros.loc[df_centros["centro"] == centro_sel_l, "n_instalacion_cnd"].values[0]

                with col_c2:
                    filtro_anio = st.selectbox(
                        "📅 Año:",
                        ["Todos", "2026", "2025"],
                        index=0,
                        key="sb_anio_historico"
                    )

                with col_c3:
                    filtro_usuario = st.text_input(
                        "🔍 Nº Usuario CND (Opcional):",
                        placeholder="Ej: 109, 001...",
                        key="ti_usuario_historico"
                    )

                # --------------------------------------------------------------
                # 2. CONSTRUIR CONSULTA SQL SELECCIONANDO EL ÚLTIMO REGISTRO (MAX(id))
                # --------------------------------------------------------------
                try:
                    where_clauses = ["l.n_instalacion_cnd = ?"]
                    params_historico = [str(n_inst_sel)]

                    if filtro_anio != "Todos":
                        where_clauses.append("l.anio = ?")
                        params_historico.append(int(filtro_anio))

                    if filtro_usuario.strip():
                        where_clauses.append("l.n_usuario_cnd = ?")
                        params_historico.append(filtro_usuario.strip())

                    where_sql = " AND ".join(where_clauses)

                    # Subconsulta que selecciona únicamente la fila con el ID más reciente
                    query_historico = f"""
                        SELECT 
                            l.n_instalacion_cnd AS 'Instalación CND',
                            l.n_usuario_cnd AS 'Usuario / Historia CND',
                            (l.mes || ' ' || CAST(l.anio AS TEXT)) AS 'Mes / Periodo',
                            l.hp10_asignada AS 'Hp(10) Leída (mSv)',
                            l.hp007_asignada AS 'Hp(0,07) Leída (mSv)'
                        FROM lecturas l
                        WHERE l.id IN (
                            SELECT MAX(id)
                            FROM lecturas
                            WHERE {where_sql}
                            GROUP BY n_instalacion_cnd, n_usuario_cnd, mes, anio
                        )
                        ORDER BY l.anio DESC, l.id DESC
                    """

                    # ----------------------------------------------------------
                    # 3. MOSTRAR TABLA Y DESCARGA
                    # ----------------------------------------------------------
                    df_historico = pd.read_sql(query_historico, conn, params=params_historico)

                    if not df_historico.empty:
                        st.caption(f"Mostrando {len(df_historico)} lecturas únicas para {centro_sel_l} (Instalación: {n_inst_sel}).")
                        
                        st.dataframe(
                            df_historico,
                            use_container_width=True,
                            hide_index=True
                        )

                        csv = df_historico.to_csv(index=False).encode('utf-8')
                        st.download_button(
                            label="📥 Descargar Consulta en CSV",
                            data=csv,
                            file_name=f"historico_dosimetria_{str(centro_sel_l).lower().replace(' ', '_')}.csv",
                            mime="text/csv"
                        )
                    else:
                        st.info(f"No se encontraron registros de lecturas para {centro_sel_l} con los filtros seleccionados.")

                except Exception as e:
                    st.error(f"Error al consultar el histórico de lecturas: {e}")
            else:
                st.warning("No hay centros disponibles según su rol de usuario.")

    else:
                    st.warning("No hay centros disponibles según su rol de usuario.")











conn.close()