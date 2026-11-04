import { useEffect, useRef, useState } from "react";
import { useStore } from "../store";

const RETRY_MS = 3000;

async function startWhep(video: HTMLVideoElement, signal: AbortSignal) {
  const connection = new RTCPeerConnection();
  const closed = new Promise<void>((resolve) => {
    connection.onconnectionstatechange = () => {
      if (["failed", "closed", "disconnected"].includes(connection.connectionState)) resolve();
    };
    signal.addEventListener("abort", () => resolve());
  });

  connection.addTransceiver("video", { direction: "recvonly" });
  connection.ontrack = (event) => {
    video.srcObject = event.streams[0] ?? new MediaStream([event.track]);
  };

  const offer = await connection.createOffer();
  await connection.setLocalDescription(offer);
  // Gathering is local and quick: the answer carries everything, so no trickle ICE is needed.
  await new Promise<void>((resolve) => {
    if (connection.iceGatheringState === "complete") return resolve();
    connection.onicegatheringstatechange = () => {
      if (connection.iceGatheringState === "complete") resolve();
    };
    setTimeout(resolve, 1000);
  });

  const response = await fetch("/video/whep", {
    method: "POST",
    headers: { "Content-Type": "application/sdp" },
    body: connection.localDescription?.sdp,
    signal,
  });
  if (!response.ok) {
    connection.close();
    throw new Error(`WHEP ${response.status}`);
  }
  await connection.setRemoteDescription({ type: "answer", sdp: await response.text() });
  await closed;
  connection.close();
  video.srcObject = null;
}

export function VideoPanel() {
  const video = useRef<HTMLVideoElement>(null);
  const hasVideo = useStore((s) => s.status.video !== null);
  const packets = useStore((s) => s.status.video?.packets ?? 0);
  const [playing, setPlaying] = useState(false);

  useEffect(() => {
    if (!hasVideo || !video.current) return;
    const controller = new AbortController();
    const element = video.current;
    const loop = async () => {
      while (!controller.signal.aborted) {
        try {
          await startWhep(element, controller.signal);
        } catch {
          // no stream yet: MediaMTX answers 404 until packets arrive
        }
        setPlaying(false);
        if (!controller.signal.aborted) {
          await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
        }
      }
    };
    void loop();
    return () => controller.abort();
  }, [hasVideo]);

  return (
    <div className="panel fill video-panel">
      <video
        ref={video}
        autoPlay
        muted
        playsInline
        onPlaying={() => setPlaying(true)}
        aria-label="Obraz z drona"
      />
      {!playing && (
        <div className="overlay-message">
          {!hasVideo
            ? "Ten profil nie ma obrazu."
            : packets === 0
              ? "Brak obrazu: nie dochodzą żadne pakiety wideo."
              : "Odbieram pakiety, uruchamiam podgląd…"}
        </div>
      )}
    </div>
  );
}
