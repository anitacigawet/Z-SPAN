[BORRADOR TEMPORAL REDACTADO POR IA. SE REESCRIBIRÁ A MÁS TARDAR EL 4 DE AGOSTO DE 2026]

# Z-SPAN

[English](README.md) · [العربية](README.ar.md) · [**Español**](README.es.md) · [فارسی](README.fa.md) · [Français](README.fr.md) · [हिन्दी](README.hi.md) · [Bahasa Indonesia](README.id.md) · [Filipino](README.fil.md) · [Português (Brasil)](README.pt-BR.md) · [Kiswahili](README.sw.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [Tiếng Việt](README.vi.md)

**Una biblioteca virtual sobre política local.**

[Visita Z-SPAN en zspan.org](https://zspan.org)

✨ **Publicada para su consulta, conservación e inspiración.**

Z-SPAN busca facilitar que cualquier persona pueda encontrar, ver y entender
las reuniones públicas locales. Los lugares se presentan como canales, las
reuniones como episodios, y los videos, órdenes del día y actas originales
siguen formando parte del recorrido.

Este repositorio es la biblioteca detrás de la biblioteca: una selección de
código fuente público, patrones de proyecto y aprendizajes que pueden servir a
quienes estén pensando en un proyecto parecido en otra ciudad, estado o país.

No es una copia completa del sistema en producción ni está pensado para
clonarse y lanzarse como otra instancia de Z-SPAN. Lo útil aquí es más pequeño:
una idea de navegación, un límite claro para la reproducción, una forma de
mantener visibles las fuentes o un principio de diseño que pueda trasladarse a
un proyecto independiente.

El [Respawn Kernel](respawn-kernel/README.md) es la excepción ejecutable: un
punto de partida independiente para crear una biblioteca de reuniones públicas
en cualquier país. La guía técnica completa está disponible por ahora en inglés.

> Esta es una traducción del README en inglés realizada con ayuda de IA. Se
> agradecen correcciones mediante pull request de personas que dominen el
> español. Si existe alguna diferencia de significado, prevalecen el
> [README en inglés](README.md), la [LICENSE](LICENSE) y el [NOTICE](NOTICE).
> Los demás documentos enlazados todavía están en inglés.

---

## 📚 Por qué existe esta biblioteca

Los proyectos que trabajan con registros públicos locales suelen encontrarse
con las mismas preguntas:

- ¿Cómo puede una persona explorar reuniones cuando cada sitio gubernamental
  las organiza de forma distinta?
- ¿Cómo puede una misma interfaz seguir siendo útil entre distintas ciudades
  y plataformas de video?
- ¿Cómo se mantiene claro el camino de regreso a una fuente oficial?
- ¿Cómo puede un sistema técnico explicarse sin obligar a la gente a leer la
  base de datos que hay debajo?

Z-SPAN es una respuesta práctica, no la única. Este repositorio busca mantener
sus ideas útiles lo bastante visibles como para que otras personas puedan
examinarlas, cuestionarlas y llevarlas más lejos en sus propios proyectos.

## 👋 Para quién es esta biblioteca

Ya seas estudiante, activista, periodista, investigador, diseñador,
desarrollador, voluntario o simplemente tengas curiosidad por la información
pública local, no necesitas adoptar el proyecto entero para encontrar algo
útil aquí. La biblioteca está organizada para que cada idea o componente pueda
entenderse por separado.

## 🧭 Cómo usar este repositorio

No hay un orden de lectura obligatorio, pero estos son buenos puntos de
entrada:

1. Lee [el modelo del proyecto](docs/PROJECT_MODEL.md) para obtener la
   explicación más sencilla de cómo se relacionan sus partes.
2. Abre [el catálogo de la biblioteca](CATALOG.md) y elige una sección de
   código, prompts o diseño según la pregunta que quieras explorar.
3. Consulta [los patrones que pueden servir en otros proyectos](docs/DESIGN_PATTERNS.md)
   para conocer las ideas que hay detrás de la interfaz.
4. Usa [la guía del repositorio](docs/REPOSITORY_GUIDE.md) para seguir un
   recorrido concreto de una persona visitante por el código publicado.
5. Revisa [qué se publica y qué no](PUBLICATION_SCOPE.md) antes de sacar
   conclusiones sobre el sistema más amplio de Z-SPAN.
6. Consulta [el registro de la instantánea actual](docs/snapshots/2026-08-02.md)
   para conocer el tamaño exacto y el estado de revisión de esta publicación.

## 🗂️ Qué hay en la colección

El código publicado muestra actualmente seis partes de la experiencia de una
persona visitante:

- **Encontrar un lugar o una reunión** mediante las vistas de inicio, canales,
  ciudades y búsqueda.
- **Explorar lo que está disponible** mediante una guía que alterna entre
  tarjetas, un mapa, un reproductor integrado y una vista ampliada.
- **Volver a los registros originales** mediante enlaces visibles a videos,
  órdenes del día y actas oficiales cuando estén disponibles.
- **Reproducir video con una interfaz común** aunque cambie la plataforma que
  aloja la grabación.
- **Explicar las comprobaciones de integridad a los visitantes** mediante las vistas de
  auditoría, escaneo y verificación.
- **Convertir el registro de una reunión en un resumen de asuntos públicos
  fácil de leer** mediante tres ejemplos de prompts examinados que se conservan
  en la colección de prompts.

[AQUÍ HABRÁ UNA PRESENTACIÓN VISUAL]

[La guía del repositorio](docs/REPOSITORY_GUIDE.md) relaciona cada una de estas
ideas con los archivos correspondientes.

## Una nota sobre la ejecución del código

En este repositorio no encontrarás instrucciones de instalación, alojamiento,
Docker o despliegue. Es una decisión deliberada.

Los archivos publicados fueron seleccionados de un sistema de trabajo privado
más amplio. Algunas importaciones, servicios, conexiones de la aplicación y
configuraciones de ejecución no están incluidos. El código se publica para su
lectura y estudio; no se presenta como una aplicación independiente ni como
una distribución con soporte.

## Cómo está organizado el repositorio

- [`docs/`](docs/) explica el modelo del proyecto, los patrones reutilizables,
  las rutas de lectura y las instantáneas públicas con fecha.
- [`code/`](code/) contiene el código de referencia seleccionado de la
  interfaz para visitantes, separado de la ruta privada de trabajo.
- [`prompts/`](prompts/) contiene tres ejemplos examinados y sin modificar que
  pueden estudiarse o adaptarse por separado.
- [`CATALOG.md`](CATALOG.md) es el índice sección por sección para personas y
  lectores de IA.
- [`PUBLICATION_SCOPE.md`](PUBLICATION_SCOPE.md) explica con claridad el límite
  de lo publicado.

La exportación pública solo cambia los nombres de las secciones. La estructura
relativa dentro de `code/visitor-interface/src/` se conserva para que sigan
siendo legibles las relaciones entre páginas, componentes, adaptadores del
reproductor y estilos.

## ⚖️ Licencia

El código publicado está disponible bajo la
[PolyForm Noncommercial License 1.0.0](LICENSE). Puede estudiarse, adaptarse,
compartirse y reutilizarse con fines no comerciales de acuerdo con sus
términos. Esto incluye el estudio personal, proyectos por afición, educación,
investigación pública, trabajo benéfico y uso gubernamental.

La licencia no concede permiso para uso comercial. La atribución obligatoria
y los límites de uso del nombre Z-SPAN están recogidos en el [NOTICE](NOTICE).

## Contacto

El proyecto está alojado en [zspan.org](https://zspan.org). Si te interesa
ocupar una plaza disponible en el ecosistema Z-SPAN, escribe a
[anitacigawet@pm.me](mailto:anitacigawet@pm.me) para obtener más información.
