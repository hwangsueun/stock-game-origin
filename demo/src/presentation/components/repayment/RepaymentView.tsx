import { useState } from "react";
import type { GameSnapshot } from "../../../application/dto/GameSnapshot";
import type { RepaymentResult } from "../../../domain/loan/RepaymentResult";
import { PixelPanel } from "../layout/PixelPanel";

type RepaymentViewProps = {
  snapshot: GameSnapshot;
  onRepay: (amount: number) => RepaymentResult;
};

const REPAYMENT_LINES = [
  {
    speaker: "사채업자",
    text: "에이~ 박사장. 20번이나 기회를 줬으면 이제 성의는 보여야지.",
  },
  {
    speaker: "나",
    text: "지갑 안의 현금과 남은 빚을 비교했다. 여기서 얼마나 갚느냐가 다음 평가를 결정한다.",
  },
];

export function RepaymentView({ snapshot, onRepay }: RepaymentViewProps) {
  const [lineIndex, setLineIndex] = useState(0);
  const [amount, setAmount] = useState(
    Math.min(snapshot.cash, snapshot.remainingDebt),
  );

  const isStoryDone = lineIndex >= REPAYMENT_LINES.length;
  const currentLine = REPAYMENT_LINES[lineIndex];

  const handleNextStory = () => {
    setLineIndex((prev) => prev + 1);
  };

  const handleRepay = () => {
    onRepay(amount);
  };

  return (
    <div style={containerStyle}>
      <PixelPanel title="상환일">
        {!isStoryDone ? (
          <>
            <div style={dialogHeaderStyle}>{currentLine.speaker}</div>
            <p style={dialogTextStyle}>{currentLine.text}</p>
            <button onClick={handleNextStory} style={buttonStyle}>
              다음
            </button>
          </>
        ) : (
          <>
            <h3>상환 금액 입력</h3>

            <div style={summaryGridStyle}>
              <SummaryItem
                label="현재 현금"
                value={`${snapshot.cash.toLocaleString()}원`}
              />
              <SummaryItem
                label="남은 빚"
                value={`${snapshot.remainingDebt.toLocaleString()}원`}
              />
              <SummaryItem
                label="최대 상환 가능"
                value={`${Math.min(
                  snapshot.cash,
                  snapshot.remainingDebt,
                ).toLocaleString()}원`}
              />
            </div>

            <label style={labelStyle}>
              상환 금액
              <input
                type="number"
                min={0}
                max={Math.min(snapshot.cash, snapshot.remainingDebt)}
                step={10000}
                value={amount}
                onChange={(e) => setAmount(Number(e.target.value))}
                style={inputStyle}
              />
            </label>

            <button onClick={handleRepay} style={buttonStyle}>
              상환하기
            </button>
          </>
        )}
      </PixelPanel>
    </div>
  );
}

function SummaryItem({ label, value }: { label: string; value: string }) {
  return (
    <div style={summaryItemStyle}>
      <span style={summaryLabelStyle}>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

const containerStyle: React.CSSProperties = {
  maxWidth: "820px",
  margin: "0 auto",
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

const summaryGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
  gap: "10px",
  marginBottom: "18px",
};

const summaryItemStyle: React.CSSProperties = {
  border: "1px solid #555",
  background: "#1d1d1d",
  padding: "10px",
};

const summaryLabelStyle: React.CSSProperties = {
  display: "block",
  color: "#bdbdbd",
  marginBottom: "4px",
};

const labelStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "6px",
  marginBottom: "14px",
};

const inputStyle: React.CSSProperties = {
  padding: "10px",
  border: "2px solid #555",
  background: "#111",
  color: "#f5f5f5",
};

const buttonStyle: React.CSSProperties = {
  padding: "10px 16px",
  border: "2px solid #f7e72f",
  background: "#fbaf45",
  color: "#171717",
  fontWeight: 800,
  cursor: "pointer",
};