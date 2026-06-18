import type { GameSnapshot } from "../../../application/dto/GameSnapshot";
import { PixelPanel } from "../layout/PixelPanel";

type DemoResultViewProps = {
  snapshot: GameSnapshot;
};

export function DemoResultView({ snapshot }: DemoResultViewProps) {
  return (
    <div style={containerStyle}>
      <PixelPanel title="데모 결과">
        <div style={resultGridStyle}>
          <ResultItem
            label="최종 현금"
            value={`${snapshot.cash.toLocaleString()}원`}
          />
          <ResultItem
            label="투자 평가금액"
            value={`${snapshot.investmentValue.toLocaleString()}원`}
          />
          <ResultItem
            label="총자산"
            value={`${snapshot.totalAssets.toLocaleString()}원`}
          />
          <ResultItem
            label="남은 빚"
            value={`${snapshot.remainingDebt.toLocaleString()}원`}
          />
          <ResultItem
            label="순자산"
            value={`${snapshot.netAssets.toLocaleString()}원`}
          />
          <ResultItem label="스트레스" value={`${snapshot.stress} / 100`} />
          <ResultItem label="신뢰도" value={`${snapshot.trust} / 100`} />
        </div>
      </PixelPanel>

      <PixelPanel title="상환 후 이야기">
        {snapshot.remainingDebt <= 0 ? (
          <p style={storyTextStyle}>
            사채업자는 돈 봉투를 세어보더니 피식 웃었다. “박사장, 의외로
            약속을 지키는 사람이었네.” 적어도 오늘 밤은 조용히 잘 수 있을 것
            같다.
          </p>
        ) : (
          <p style={storyTextStyle}>
            사채업자의 표정이 애매하게 굳었다. “에이, 박사장. 이걸로 끝난 줄
            알면 섭섭하지.” 빚은 줄었지만, 완전히 끝난 것은 아니다.
          </p>
        )}
      </PixelPanel>

      <PixelPanel title="최종 로그">
        <div style={logBoxStyle}>
          {snapshot.logs
            .slice()
            .reverse()
            .map((log) => (
              <p key={log.id} style={logLineStyle}>
                [T{log.turnIndex}] {log.message}
              </p>
            ))}
        </div>
      </PixelPanel>
    </div>
  );
}

function ResultItem({ label, value }: { label: string; value: string }) {
  return (
    <div style={resultItemStyle}>
      <span style={resultLabelStyle}>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

const containerStyle: React.CSSProperties = {
  maxWidth: "960px",
  margin: "0 auto",
};

const resultGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
  gap: "10px",
};

const resultItemStyle: React.CSSProperties = {
  border: "1px solid #555",
  background: "#1d1d1d",
  padding: "12px",
};

const resultLabelStyle: React.CSSProperties = {
  display: "block",
  color: "#bdbdbd",
  marginBottom: "4px",
};

const storyTextStyle: React.CSSProperties = {
  fontSize: "18px",
  lineHeight: 1.7,
};

const logBoxStyle: React.CSSProperties = {
  maxHeight: "220px",
  overflowY: "auto",
};

const logLineStyle: React.CSSProperties = {
  borderBottom: "1px solid #333",
  paddingBottom: "6px",
  marginBottom: "6px",
};