import { useState } from "react";
import type { CSSProperties } from "react";
import type { GameSnapshot } from "../../../application/dto/GameSnapshot";
import { PixelPanel } from "../layout/PixelPanel";
import { NewsPanel } from "../news/NewsPanel";

type InvestmentDashboardProps = {
  snapshot: GameSnapshot;
  onBuy: (assetId: string, amount: number) => void;
  onSellAll: (assetId: string) => void;
  onHold: () => void;
  onEndTurn: () => void;
};

export function InvestmentDashboard({
  snapshot,
  onBuy,
  onSellAll,
  onHold,
  onEndTurn,
}: InvestmentDashboardProps) {
  const [selectedAssetId, setSelectedAssetId] = useState(
    snapshot.marketRows[0]?.assetId ?? "",
  );

  const [buyAmount, setBuyAmount] = useState(100000);

  const selectedAsset = snapshot.marketRows.find(
    (row) => row.assetId === selectedAssetId,
  );

  const handleBuy = () => {
    if (!selectedAssetId) return;
    onBuy(selectedAssetId, buyAmount);
  };

  const handleSellAll = () => {
    if (!selectedAssetId) return;
    onSellAll(selectedAssetId);
  };

  return (
    <div style={dashboardStyle}>
      <PixelPanel title="상태">
        <div style={statusGridStyle}>
          <StatusItem label="날짜" value={snapshot.currentDate} />

          <StatusItem
            label="투자 턴"
            value={`${snapshot.investmentTurn} / ${snapshot.maxTurn}`}
          />

          <StatusItem
            label="현금"
            value={`${snapshot.cash.toLocaleString()}원`}
          />

          <StatusItem
            label="투자 평가금액"
            value={`${snapshot.investmentValue.toLocaleString()}원`}
          />

          <StatusItem
            label="총자산"
            value={`${snapshot.totalAssets.toLocaleString()}원`}
          />

          <StatusItem
            label="남은 빚"
            value={`${snapshot.remainingDebt.toLocaleString()}원`}
          />

          <StatusItem label="스트레스" value={`${snapshot.stress} / 100`} />

          <StatusItem label="신뢰도" value={`${snapshot.trust} / 100`} />
        </div>
      </PixelPanel>

      <div style={mainGridStyle}>
        <PixelPanel title="시장">
          <div style={assetGridStyle}>
            {snapshot.marketRows.map((row) => {
              const isSelected = selectedAssetId === row.assetId;

              return (
                <button
                  key={row.assetId}
                  onClick={() => setSelectedAssetId(row.assetId)}
                  style={{
                    ...assetButtonStyle,
                    borderColor: isSelected ? "#f7e72f" : "#555",
                    boxShadow: isSelected ? "4px 4px 0 #000" : "none",
                  }}
                >
                  <strong style={assetNameStyle}>{row.name}</strong>

                  <span style={assetMetaStyle}>{row.assetType}</span>

                  <span style={priceStyle}>
                    {row.closePrice.toLocaleString()}원
                  </span>

                  <span
                    style={{
                      ...changeRateStyle,
                      color: row.changeRate >= 0 ? "#ff7675" : "#74b9ff",
                    }}
                  >
                    {row.changeRate >= 0 ? "+" : ""}
                    {row.changeRate.toFixed(2)}%
                  </span>
                </button>
              );
            })}
          </div>
        </PixelPanel>

        <PixelPanel title="거래">
          {selectedAsset ? (
            <>
              <h3 style={selectedTitleStyle}>{selectedAsset.name}</h3>

              <div style={tradeInfoBoxStyle}>
                <p style={tradeInfoLineStyle}>
                  <span>현재가</span>
                  <strong>{selectedAsset.closePrice.toLocaleString()}원</strong>
                </p>

                <p style={tradeInfoLineStyle}>
                  <span>등락률</span>
                  <strong
                    style={{
                      color:
                        selectedAsset.changeRate >= 0 ? "#ff7675" : "#74b9ff",
                    }}
                  >
                    {selectedAsset.changeRate >= 0 ? "+" : ""}
                    {selectedAsset.changeRate.toFixed(2)}%
                  </strong>
                </p>

                <p style={tradeInfoLineStyle}>
                  <span>자산 구분</span>
                  <strong>{selectedAsset.assetType}</strong>
                </p>
              </div>

              <label style={labelStyle}>
                매수 금액
                <input
                  type="number"
                  value={buyAmount}
                  min={0}
                  step={10000}
                  onChange={(e) => setBuyAmount(Number(e.target.value))}
                  style={inputStyle}
                />
              </label>

              <div style={quickAmountRowStyle}>
                <button
                  onClick={() => setBuyAmount(100000)}
                  style={quickButtonStyle}
                >
                  10만
                </button>

                <button
                  onClick={() => setBuyAmount(300000)}
                  style={quickButtonStyle}
                >
                  30만
                </button>

                <button
                  onClick={() => setBuyAmount(500000)}
                  style={quickButtonStyle}
                >
                  50만
                </button>

                <button
                  onClick={() => setBuyAmount(snapshot.cash)}
                  style={quickButtonStyle}
                >
                  전액
                </button>
              </div>

              <div style={buttonRowStyle}>
                <button onClick={handleBuy} style={primaryButtonStyle}>
                  금액 매수
                </button>

                <button onClick={handleSellAll} style={primaryButtonStyle}>
                  전량 매도
                </button>
              </div>

              <div style={buttonRowStyle}>
                <button onClick={onHold} style={secondaryButtonStyle}>
                  보유 후 다음 턴
                </button>

                <button onClick={onEndTurn} style={secondaryButtonStyle}>
                  거래 완료
                </button>
              </div>
            </>
          ) : (
            <p>선택된 종목이 없습니다.</p>
          )}
        </PixelPanel>
      </div>

      <div style={mainGridStyle}>
        <PixelPanel title="포트폴리오">
          {snapshot.holdings.length === 0 ? (
            <p style={mutedTextStyle}>보유 종목 없음</p>
          ) : (
            <table style={tableStyle}>
              <thead>
                <tr>
                  <th style={thStyle}>종목</th>
                  <th style={thStyle}>수량</th>
                  <th style={thStyle}>평단가</th>
                  <th style={thStyle}>현재가</th>
                  <th style={thStyle}>평가금액</th>
                  <th style={thStyle}>손익</th>
                  <th style={thStyle}>수익률</th>
                </tr>
              </thead>

              <tbody>
                {snapshot.holdings.map((item) => (
                  <tr key={item.assetId}>
                    <td style={tdStyle}>{item.name}</td>

                    <td style={tdStyle}>{item.quantity}</td>

                    <td style={tdStyle}>
                      {item.averageBuyPrice.toLocaleString()}원
                    </td>

                    <td style={tdStyle}>
                      {item.currentPrice.toLocaleString()}원
                    </td>

                    <td style={tdStyle}>
                      {item.evaluatedValue.toLocaleString()}원
                    </td>

                    <td
                      style={{
                        ...tdStyle,
                        color:
                          item.unrealizedPnl >= 0 ? "#ff7675" : "#74b9ff",
                      }}
                    >
                      {item.unrealizedPnl >= 0 ? "+" : ""}
                      {item.unrealizedPnl.toLocaleString()}원
                    </td>

                    <td
                      style={{
                        ...tdStyle,
                        color:
                          item.unrealizedReturnRate >= 0
                            ? "#ff7675"
                            : "#74b9ff",
                      }}
                    >
                      {item.unrealizedReturnRate >= 0 ? "+" : ""}
                      {(item.unrealizedReturnRate * 100).toFixed(2)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </PixelPanel>

        <NewsPanel
          turnIndex={snapshot.investmentTurn}
          selectedAssetId={selectedAssetId}
        />
      </div>

      <PixelPanel title="로그">
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

function StatusItem({ label, value }: { label: string; value: string }) {
  return (
    <div style={statusItemStyle}>
      <span style={statusLabelStyle}>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

const dashboardStyle: CSSProperties = {
  maxWidth: "1280px",
  margin: "0 auto",
};

const statusGridStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
  gap: "10px",
};

const statusItemStyle: CSSProperties = {
  border: "1px solid #555",
  padding: "10px",
  background: "#1d1d1d",
};

const statusLabelStyle: CSSProperties = {
  display: "block",
  color: "#bdbdbd",
  fontSize: "13px",
  marginBottom: "4px",
};

const mainGridStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "1.4fr 1fr",
  gap: "18px",
};

const assetGridStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
  gap: "10px",
};

const assetButtonStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "6px",
  padding: "12px",
  border: "2px solid #555",
  background: "#1d1d1d",
  color: "#f5f5f5",
  textAlign: "left",
  cursor: "pointer",
};

const assetNameStyle: CSSProperties = {
  fontSize: "15px",
};

const assetMetaStyle: CSSProperties = {
  color: "#bdbdbd",
  fontSize: "12px",
};

const priceStyle: CSSProperties = {
  fontSize: "16px",
  fontWeight: 800,
};

const changeRateStyle: CSSProperties = {
  fontSize: "14px",
  fontWeight: 800,
};

const selectedTitleStyle: CSSProperties = {
  marginTop: 0,
  marginBottom: "12px",
  color: "#f7e72f",
};

const tradeInfoBoxStyle: CSSProperties = {
  border: "1px solid #555",
  background: "#1d1d1d",
  padding: "10px",
  marginBottom: "14px",
};

const tradeInfoLineStyle: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  gap: "10px",
  margin: "0 0 8px",
};

const labelStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "6px",
  marginBottom: "12px",
};

const inputStyle: CSSProperties = {
  padding: "10px",
  border: "2px solid #555",
  background: "#111",
  color: "#f5f5f5",
};

const quickAmountRowStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
  gap: "8px",
  marginBottom: "10px",
};

const quickButtonStyle: CSSProperties = {
  padding: "8px",
  border: "1px solid #777",
  background: "#2a2a2a",
  color: "#f5f5f5",
  fontWeight: 700,
  cursor: "pointer",
};

const buttonRowStyle: CSSProperties = {
  display: "flex",
  gap: "8px",
  marginTop: "10px",
};

const primaryButtonStyle: CSSProperties = {
  flex: 1,
  padding: "10px",
  border: "2px solid #f7e72f",
  background: "#fbaf45",
  color: "#171717",
  fontWeight: 800,
  cursor: "pointer",
};

const secondaryButtonStyle: CSSProperties = {
  flex: 1,
  padding: "10px",
  border: "2px solid #777",
  background: "#333",
  color: "#f5f5f5",
  fontWeight: 700,
  cursor: "pointer",
};

const mutedTextStyle: CSSProperties = {
  color: "#bdbdbd",
};

const tableStyle: CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
};

const thStyle: CSSProperties = {
  borderBottom: "2px solid #555",
  padding: "8px",
  textAlign: "left",
  color: "#f7e72f",
  fontSize: "13px",
};

const tdStyle: CSSProperties = {
  borderBottom: "1px solid #444",
  padding: "8px",
  fontSize: "13px",
};

const logBoxStyle: CSSProperties = {
  maxHeight: "180px",
  overflowY: "auto",
};

const logLineStyle: CSSProperties = {
  borderBottom: "1px solid #333",
  paddingBottom: "6px",
  marginBottom: "6px",
  color: "#dcdcdc",
};