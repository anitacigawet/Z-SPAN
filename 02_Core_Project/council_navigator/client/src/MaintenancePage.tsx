import "./MaintenancePage.css";

export default function MaintenancePage() {
  return (
    <main className="maintenance-page" aria-labelledby="maintenance-title">
      <section className="maintenance-card">
        <img
          className="maintenance-cat"
          src="/states/sleeping-cat.png"
          alt="A sleeping cat"
          draggable={false}
        />
        <h1 id="maintenance-title">Z-SPAN is undergoing maintenance.</h1>
        <p className="maintenance-message">
          Please check back later. Thank you for your time.
        </p>
      </section>
    </main>
  );
}
