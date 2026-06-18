import { useState } from "react";
import type { GameSnapshot } from "../../../application/dto/GameSnapshot";
import { PixelPanel } from "../layout/PixelPanel";

type StartStoryViewProps = {
  snapshot: GameSnapshot;
  onComplete: () => void;
};

const START_LINES = [
  {
    speaker: "나",
    text: "오늘도 별일 없는 하루라고 생각했다. 적어도 전화가 오기 전까지는.",
  },
  {
    speaker: "사채업자",
    text: "에이, 박사장. 약속한 날짜가 다가오는데 얼굴 한 번 봐야지?",
  },
  {
    speaker: "나",
    text: "남은 빚은 500만 원. 앞으로 20번의 투자 기회 안에 최대한 돈을 불려야 한다.",
  },
  {
    speaker: "시스템",
    text: "투자는 총 20턴 동안 진행된다. 시작 스토리는 투자 턴에 포함되지 않는다.",
  },
];

export function StartStoryView({ snapshot, onComplete }: StartStoryViewProps) {
  const [lineIndex, setLineIndex] = useState(0);
  const currentLine = START_LINES[lineIndex];
  const isLastLine = lineIndex === START_LINES.length - 1;

  const handleNext = () => {
    if (isLastLine) {
      onComplete();
      return;
    }

    setLineIndex((prev) => prev + 1);
  };

  return (
    <div style={containerStyle}>
      <PixelPanel title="시작 스토리">
        <div style={roomStyle}>
          <div style={characterStyle}>😐</div>
          <div>
            <p style={smallTextStyle}>현재 날짜</p>
            <h3 style={{ marginTop: 0 }}>{snapshot.currentDate}</h3>
            <p style={smallTextStyle}>초기 현금</p>
            <h3 style={{ marginTop: 0 }}>{snapshot.cash.toLocaleString()}원</h3>
            <p style={smallTextStyle}>초기 부채</p>
            <h3 style={{ marginTop: 0 }}>
              {snapshot.remainingDebt.toLocaleString()}원
            </h3>
          </div>
        </div>
      </PixelPanel>

      <PixelPanel>
        <div style={dialogHeaderStyle}>{currentLine.speaker}</div>
        <p style={dialogTextStyle}>{currentLine.text}</p>

        <button onClick={handleNext} style={buttonStyle}>
          {isLastLine ? "투자 시작" : "다음"}
        </button>
      </PixelPanel>
    </div>
  );
}

const containerStyle: React.CSSProperties = {
  maxWidth: "900px",
  margin: "0 auto",
};

const roomStyle: React.CSSProperties = {
  minHeight: "260px",
  display: "grid",
  gridTemplateColumns: "1fr 1fr",
  gap: "20px",
  alignItems: "center",
};

const characterStyle: React.CSSProperties = {
  fontSize: "96px",
  textAlign: "center",
};

const smallTextStyle: React.CSSProperties = {
  color: "#bdbdbd",
  marginBottom: "4px",
};

const dialogHeaderStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "6px 10px",
  border: "2px solid #f7e72f",
  color: "#f7e72f",
  fontWeight: 800,
  marginBottom: "10px",
};

const dialogTextStyle: React.CSSProperties = {
  fontSize: "18px",
  lineHeight: 1.6,
};

const buttonStyle: React.CSSProperties = {
  padding: "10px 16px",
  border: "2px solid #f7e72f",
  background: "#fbaf45",
  color: "#171717",
  fontWeight: 800,
  cursor: "pointer",
};