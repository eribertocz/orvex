# Resumen para el Owner (español)

Este documento resume, en lenguaje llano, lo que dicen los documentos en inglés. **Si algo difiere, mandan los documentos en inglés** (orden de prioridad en `docs/README.md`).

## 1. Qué es Orvex

Una herramienta BI propia, de código abierto (Apache-2.0), que:

- se conecta a Parquet, CSV, DuckDB, SQLite, PostgreSQL y ClickHouse;
- transforma datos con **SQL puro** y los guarda en **DuckLake**;
- define el significado del negocio en **Apache Ossie**;
- tiene un **motor semántico** que garantiza números correctos o rechaza la pregunta con un error claro;
- muestra dashboards con páginas, gráficos, KPIs, filtros y la **matriz**;
- comparte por URL con roles, grupos y seguridad por página, fila y columna;
- explica cada número ("¿De dónde sale?");
- se usa tan fácil como DuckDB: `orvex` y listo.

## 2. Usuarios

| Rol | Quién | Qué hace |
|---|---|---|
| Builder | Tú | Todo en un proyecto: conexiones, modelos, métricas, seguridad, miembros y grupos (los usuarios los crea el administrador de la instancia, que también eres tú) |
| Author | Jefaturas | Arma dashboards con tus métricas certificadas; puede proponer métricas |
| Viewer | Gerente y personal | Ve las páginas que tiene permitidas |

## 3. Las reglas que nunca se rompen

13 leyes (verdad única, transparencia, seguridad por diseño, consistencia, separación de capas, niveles de experiencia, rendimiento predecible, gobierno del modelo, observabilidad, extensibilidad, exactitud, apertura y accesibilidad) y 12 anti-patrones prohibidos. Están en `docs/product/laws.md`. Ningún PR que viole una ley se integra.

## 4. Cómo está construido

Módulos independientes que se comunican solo por **contratos** (formatos estándar):

| Módulo | Hace |
|---|---|
| `store` | Guarda y lee datos (DuckLake) |
| `transform` | ETL: fuentes, modelos SQL, cargas incrementales, pruebas |
| `semantic` | **El núcleo:** Ossie → consultas correctas, seguras y explicadas |
| `access` | Usuarios, login, grupos, auditoría |
| `dashboards` | Definiciones de dashboards en YAML y acceso a páginas |
| `mcp` | Acceso para IA |
| `server` | Une todo; API, CLI y servidor web |
| `web` | La interfaz |

Cualquier módulo se puede reescribir (por ejemplo, en Rust) o usar por separado sin afectar a los demás.

**Dos modos:** local (un comando, sin configurar nada) y servidor (`--server` o Docker, con PostgreSQL para el catálogo de DuckLake y el estado).

**Siempre las últimas versiones estables** de todas las librerías, fijadas exactamente y actualizadas cada semana con pruebas automáticas.

## 5. El motor semántico

Iguala todo lo que hace SEF (los 21 casos del corpus, incluido el conteo distinto que SEF no ejecuta) y lo supera con:

- **S1:** % del total, nivel de detalle (LOD) y grupos de cálculo;
- **S2:** comparar dos selecciones y ver valores posibles al filtrar;
- **S3:** inteligencia temporal (año anterior, acumulado del año, calendarios de negocio);
- **S4:** creación de métricas con vista previa e impacto antes de publicarlas;
- **S5:** preagregaciones que nunca cambian el resultado;
- **S6:** recibo legible de cada número y explicación de por qué cambió una cifra;
- **Evidencia verificada contra los datos** después de cada carga: si una relación deja de cumplirse, el motor no la usa y lo dice.

## 6. ETL

Cada modelo es un `.sql` puro más un `.yaml` opcional con la configuración (incremental, pruebas, frescura). El motor agrega solo el filtro incremental cuando puede probar que es seguro; si no, se usa `$since` (DuckDB 2.0). Cada carga se escribe, se prueba y solo se publica si pasa; si falla, los dashboards siguen mostrando los últimos datos buenos con un aviso. La frescura mínima es de 5 minutos.

- **Un dialecto por motor, sin traducir SQL (ADR-0026):** los modelos se escriben en SQL de DuckDB. ClickHouse se lee con *modelos de extracción* escritos en SQL de ClickHouse que se ejecutan en ClickHouse (solo lectura) y llegan a DuckDB como Arrow.
- **Los datos se quedan en DuckDB (ADR-0025):** relaciones perezosas, nada se materializa en Python, cada resultado se lee una sola vez y en un snapshot fijo.

## 7. Quién hace qué

Un solo agente, **Claude Code**, especifica **e** implementa. Trabaja con tu cuenta de GitHub y firma sus commits como `Claude Code <claude-code@orvex.invalid>`.

| Acción | Tú (Owner) | Agente |
|---|---|---|
| Decisiones de producto | **Sí** | Propone con recomendación |
| Aprobar el lote de comportamientos nuevos de cada fase | **Sí** | Lo presenta en español |
| Crear issues y mantener **siempre 3 issues especificados por delante** | Informado | **Sí** |
| Ramas, commits, push, PR | — | **Sí** |
| Revisión independiente de lo crítico (motor, seguridad, contratos) | Informado | **Sí**, con un subagente “Reviewer” de contexto limpio |
| Merge de PR normales | Informado | **Sí**, con todas las pruebas en verde |
| Merge de PR que tocan rutas gobernadas (toda la carpeta `docs/` salvo la guía de usuario, `AGENTS.md`, casos normativos, CI, licencia) | **Sí, solo tú** | Lo prepara con la etiqueta `owner-merge` |
| Aprobar `beta.1`, cada `rc`, `v1.0.0` y parches estables | **Sí** (comentario `Approved for release vX.Y.Z`) | — |
| Crear tags y releases | — | **Sí** (después de tu aprobación cuando se requiere) |

- La especificación de un issue se congela antes de programar; el agente nunca cambia los criterios para que el código “pase”.
- Si el agente tiene una duda de producto, abre un issue `type:decision` con opciones, recomendación y resumen en español, y sigue con otro trabajo mientras respondes.
- Cuando algo usable se integra, te explica en español qué puedes hacer ahora y cómo probarlo.

## 8. Tamaño de los issues

**Todos los issues son medianos:** una capacidad observable, un módulo, como máximo un contrato creado o extendido, unas 300–800 líneas y alrededor de una hora de revisión. Si uno crece más, el agente lo divide antes de seguir.

## 9. Roadmap y versiones

| Fase | Qué se construye | Issues | Versión al cerrar |
|---|---|---|---|
| P0 | Base del repositorio, CI, contratos, datos sintéticos, corpus | 11 | `v0.1.0-alpha.1` |
| P1 | Almacenamiento y ETL | 21 | `v0.2.0-alpha.1` |
| P2 | Motor semántico: paridad con SEF (21/21) | 28 | `v0.3.0-alpha.1` |
| P3 | Usuarios, login, seguridad, auditoría, caché | 14 | `v0.4.0-alpha.1` |
| P4 | Motor semántico: S1, S3, S6 | 12 | `v0.5.0-alpha.1` |
| P5 | Motor semántico: S2, S4, S5 y el escritor de archivos del proyecto | 12 | `v0.6.0-alpha.1` |
| P6 | Interfaz y dashboards | 21 | `v0.7.0-alpha.1` |
| P7 | La matriz | 12 | `v0.8.0-alpha.1` |
| P8 | Estudio (editor, linaje, métricas) | 10 | `v0.9.0-alpha.1` |
| P9 | IA (MCP) y operación | 11 | `v0.10.0-alpha.1` |
| P10 | Piloto con usuarios reales | 6 | `v0.11.0-beta.N` |
| RC | Solo correcciones | — | `v1.0.0-rc.N` |
| **MVP** | **Versión estable** | — | **`v1.0.0`** |

En total son **158 issues medianos**.

- **Alpha:** solo tú; los formatos aún pueden cambiar.
- **Beta:** todo el MVP programado; piloto con tu gerente y una o dos jefaturas.
- **v1.0.0:** el MVP listo y estable, cuando se cumplen los 10 criterios de salida y tú lo apruebas.

## 10. Cómo leen los agentes la documentación

Un agente nunca lee todo: lee su issue, la **ficha de su módulo** (`docs/modules/`) y solo las secciones que esos dos indican.

## 11. Edición desde la interfaz (ADR-0024)

Los editores de código (Monaco) guardan el texto exactamente como lo escribiste, sin reformatear ni borrar comentarios. Los formularios existen solo para dashboards y métricas, y antes de guardar muestran las diferencias. Los borradores viven fuera de Git; en modo servidor cada publicación es un commit con el nombre de quien la hizo.

## 12. Después del MVP

Modelos Python y predicciones de machine learning (XGBoost, LightGBM, scikit-learn sobre Arrow, Polars y DuckDB) y visuales Python (Plotly, Matplotlib), en procesos aislados y con la misma seguridad. Ya está diseñado (ADR-0023) para no rediseñar nada después.

## 13. Lo que tienes que hacer tú

1. Crear en GitHub el repositorio **vacío** `eribertocz/orvex` (público, sin README ni licencia).
2. Revisar y fusionar el PR de fundación (`docs: project foundation`).
3. En P0: buscar que el nombre “Orvex” no tenga conflictos de marca (es renombrable en minutos si hiciera falta) y aplicar la protección de `main` que el agente te deja preparada (incluida “no permitir saltarse las reglas”, también para administradores).
4. Aprobar el lote de comportamientos nuevos al inicio de cada fase.
