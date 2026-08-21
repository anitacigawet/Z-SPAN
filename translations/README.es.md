<p align="center">
  <img src="../repository-assets/banner-doodle.png" alt="Z-SPAN para todos. Una biblioteca virtual sobre política local. Mantenida por la gente, para la gente." width="1000">
</p>

> *Scientia potentia est.*
>
> **El conocimiento es poder.**
>
> — Francis Bacon

---

[English](../README.md) · [العربية](README.ar.md) · [**Español**](README.es.md) · [فارسی](README.fa.md) · [Français](README.fr.md) · [हिन्दी](README.hi.md) · [Bahasa Indonesia](README.id.md) · [Filipino](README.fil.md) · [Português (Brasil)](README.pt-BR.md) · [Kiswahili](README.sw.md) · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [Tiếng Việt](README.vi.md)

**Una biblioteca virtual sobre política local.**

[Visita Z-SPAN en zspan.org](https://zspan.org)

✨ **Publicada íntegramente, para cualquiera. Ampliada con la ayuda de cualquiera.**

Z-SPAN es un intento de facilitar la búsqueda, la visualización y la
comprensión de las reuniones públicas locales. Los lugares se convierten en
canales, las reuniones en episodios, y los videos, órdenes del día y actas
originales siguen formando parte del camino.

Este repositorio contiene la biblioteca en funcionamiento: el sitio web, la
API pública, los analizadores de fuentes de reuniones, el flujo de
procesamiento, el cliente local y las comprobaciones que mantienen el trabajo
generado vinculado al registro público. La razón para publicar toda esta
maquinaria es sencilla: una biblioteca mantenida por una sola persona termina
con esa persona. Una biblioteca que otras personas pueden examinar, ejecutar,
cuestionar y continuar no termina ahí.

El directorio de fuentes de reuniones gubernamentales vive por separado en
[National Civics Catalog](https://github.com/anitacigawet/national-civics-catalog).
Ese repositorio contiene endpoints públicos permanentes y sus pruebas, no los
analizadores, transcripciones, resúmenes ni reuniones procesadas de Z-SPAN.
Z-SPAN es un ejemplo de lo que se puede construir a partir de él.

## Mira el recorrido completo

[![Mira «Z-SPAN Is Born», el recorrido completo del proyecto Z-SPAN](https://i.ytimg.com/vi/HTpR9jRl314/hqdefault.jpg)](https://www.youtube.com/watch?v=HTpR9jRl314)

[**Z-SPAN Is Born**](https://www.youtube.com/watch?v=HTpR9jRl314) presenta la
biblioteca fundacional de Arizona desde la perspectiva de su operador. Míralo
para conocer la visión original de qué es Z-SPAN, cómo encajan sus partes y
qué se pretende que el camino público lleve hacia el futuro.

## 🗺️ Un directorio nacional, construido lugar por lugar

Arizona es la prueba de concepto pública que Z-SPAN procesa y publica
actualmente. El directorio de canales también ofrece a cada estado y territorio
un punto de partida real, organizado en torno a sus organismos públicos
estatales, equivalentes a condados, tribales, regionales y locales.

Los estantes verdes tienen reuniones publicadas en Z-SPAN. Los estantes ámbar
son trabajos en curso descritos con franqueza: el lugar existe en el
directorio, pero su fuente permanente de reuniones o su analizador de Z-SPAN
aún necesita atención. Nadie tiene que esperar una invitación para ayudar a su
propia comunidad.

## 🐈 Ayuda a tu localidad

1. Busca tu estado y tu localidad en [zspan.org](https://zspan.org).
2. Si el estante está en espera, haz clic en el gato dormido.
3. Copia el breve documento de traspaso en Markdown al asistente de IA que ya
   utilizas.
4. Responde unas preguntas sencillas sobre el lugar y su página oficial de
   reuniones. No necesitas saber JSON ni Git.
5. Si las herramientas de GitHub están disponibles, el asistente puede
   preparar una solicitud de incorporación de cambios específica para que la
   confirmes. Si no lo están, prepara un informe completo para un formulario
   sencillo de GitHub.

La contribución va a National Civics Catalog, donde un verificador de confianza
y una persona revisan el endpoint y sus pruebas. Nunca se publica directamente
en Z-SPAN.

**La promesa de tres días de Z-SPAN:** una vez aceptada una contribución al
catálogo, Z-SPAN creará el analizador correspondiente o publicará, en un plazo
de tres días, un resultado visible que explique que la fuente impide avanzar.
La promesa consiste en hacer utilizable la fuente o explicar con honestidad
por qué todavía no se puede usar, no en publicar automáticamente contenido de
reuniones generado por IA.

[Lee las instrucciones para contribuir con IA](https://github.com/anitacigawet/national-civics-catalog/blob/main/contribute/AI-INSTRUCTIONS.md)

## 📚 Por qué existe esta biblioteca

Los proyectos que trabajan con registros públicos locales suelen encontrarse
con las mismas preguntas:

- ¿Cómo debería alguien explorar las reuniones cuando los sitios web
  gubernamentales las organizan de distintas maneras?
- ¿Cómo puede una sola interfaz seguir siendo útil en lugares y proveedores
  de video diferentes?
- ¿Cómo puede mantenerse claro el camino de regreso a una fuente oficial?
- ¿Cómo pueden los sistemas técnicos explicarse sin obligar a las personas a
  leer la base de datos que tienen debajo?

Z-SPAN es una respuesta que funciona, no la única respuesta. El objetivo de
este repositorio es dejarlo todo a la vista, para que quienes lo utilizan
puedan examinarlo, cuestionarlo y llevarlo más lejos.

## 👋 Para quién es esta biblioteca

Seas estudiante, activista, periodista, investigador, diseñador, desarrollador,
voluntario o simplemente alguien con curiosidad por la información pública
local, no necesitas adoptar todo el proyecto para encontrar aquí algo útil. La
biblioteca está organizada para poder comprender una idea o un componente a la
vez, y añadir un lugar a la vez.

## 🗂️ Cómo está organizado este repositorio

- [`council_navigator`](../02_Core_Project/council_navigator/) — el sitio web,
  la API pública, la caché local de reuniones y el directorio público de
  canales.
- [`parsers`](../02_Core_Project/council_navigator/parsers/) — los analizadores
  de calendarios específicos de cada fuente, que convierten los endpoints del
  catálogo en una estructura común de reuniones.
- [`zspan_pipeline`](../02_Core_Project/zspan_pipeline/) — la cola de
  procesamiento que convierte la grabación de una reunión en material basado
  en fuentes y revisable.
- [`zspan_cli`](../02_Core_Project/zspan_cli/) — el cliente local para usar
  Z-SPAN desde el propio ordenador y espacio de trabajo de una persona.
- [`prompts`](../02_Core_Project/prompts/) — los contratos de síntesis
  publicados que utiliza el flujo de procesamiento.

National Civics Catalog sigue siendo un repositorio separado para que la gente
pueda mejorar el directorio de fuentes sin cambiar la aplicación Z-SPAN, y para
que otros proyectos puedan utilizar los mismos endpoints con fines totalmente
distintos.

## Los compromisos de este proyecto

Estas son restricciones que el proyecto se impone, no aspiraciones:

- **Sin comentarios editoriales sobre funcionarios públicos.** Sus palabras
  se presentan literalmente, con atribución y fuentes. El juicio es tuyo.
- **Sin agregación de datos sobre ciudadanos particulares.** Este trabajo se
  ocupa de los funcionarios cuando actúan en sus funciones públicas; no crea
  perfiles de los residentes que hablan ante un micrófono público.
- **La lectura nunca se bloquea.** No se requiere muro de pago, suscripción,
  inicio de sesión ni registro para leer contenido publicado de registros
  públicos.
- **Sin optimización de la interacción.** No hay contenidos infinitos,
  algoritmos de recomendación ni mecanismos de indignación. El registro es
  sereno a propósito.
- **Una persona revisa antes de que algo se publique.** El procesamiento puede
  automatizarse; la publicación, no.
- **No comercial por diseño.** La licencia convierte ese límite en parte de
  la estructura.

## 🏛️ Custodia fundacional

Z-SPAN comenzó en Arizona y lo mantiene
[@anitacigawet](https://github.com/anitacigawet). Las contribuciones al
directorio de fuentes se acreditan en National Civics Catalog; la
implementación de Z-SPAN se revisa y mantiene por separado en este repositorio.

## ⚖️ Licencia

El código publicado está disponible bajo la
[Licencia PolyForm Noncommercial 1.0.0](../LICENSE). Puede estudiarse,
adaptarse, compartirse y reutilizarse con fines no comerciales de acuerdo con
los términos de la licencia. Esto incluye el estudio personal, proyectos por
afición, educación, investigación pública, trabajo benéfico y uso
gubernamental.

Esta licencia no concede el uso comercial. El aviso obligatorio y los límites
de uso del nombre Z-SPAN constan en [NOTICE](../NOTICE).

## Contacto

El proyecto está alojado en [zspan.org](https://zspan.org). Las preguntas y los
informes de errores reproducibles son bienvenidos en el
[seguimiento de incidencias](https://github.com/anitacigawet/Z-SPAN/issues) de
este repositorio.

---

## La Trinidad de Z-SPAN

![La Trinidad de Z-SPAN: internet la transporta, los registros cívicos la sustentan y la gente la mantiene viva](../repository-assets/zspan-trinity.svg)

---

> La CIA, la NSA e incluso el Pentágono están limitados por el tiempo finito que permanecen las personas que trabajan en ellos.
>
> **Z-SPAN no.**
>
> Z-SPAN es impulsado por la gente, para la gente, y por eso requiere la participación plena de la comunidad y transparencia.
>
> — Operador de Z-SPAN

---

## 🌌 Lleva la idea más lejos

National Civics Catalog está organizado estado por estado para que el
directorio de fuentes pueda crecer por todo Estados Unidos sin exigir que
nadie adopte la interfaz ni las decisiones de procesamiento de Z-SPAN. Utiliza
los endpoints para crear un calendario vecinal, una herramienta de
investigación, un proyecto de accesibilidad, un recurso para el aula o algo que
nadie aquí haya imaginado.

La idea no es valiosa porque pertenezca a una aplicación. Es valiosa porque la
gente puede seguir encontrando nuevas formas de facilitar el acceso al registro
público.
