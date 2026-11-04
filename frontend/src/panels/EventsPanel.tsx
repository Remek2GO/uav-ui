import { useEffect, useRef } from "react";
import { useStore } from "../store";

function clock(seconds: number) {
  return new Date(seconds * 1000).toLocaleTimeString("pl-PL", { hour12: false });
}

export function EventList() {
  const events = useStore((s) => s.events);
  const container = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  useEffect(() => {
    if (pinned.current && container.current) {
      container.current.scrollTop = container.current.scrollHeight;
    }
  }, [events]);

  if (events.length === 0) return <div className="empty">Na razie nic się nie wydarzyło.</div>;

  return (
    <div
      className="events"
      ref={container}
      role="log"
      onScroll={(event) => {
        const box = event.currentTarget;
        pinned.current = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
      }}
    >
      {events.map((event) => (
        <div key={event.id} className={`event level-${event.level}`}>
          <time>{clock(event.t)}</time>
          <span className="source">{event.source}</span>
          <span>{event.text}</span>
        </div>
      ))}
    </div>
  );
}

export function EventsPanel() {
  return (
    <div className="panel fill">
      <EventList />
    </div>
  );
}
