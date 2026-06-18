export type PricePoint = {
  assetId: string;
  turnIndex: number;
  gameDate: string;
  closePrice: number;
  changeRate: number;   // 소수점 비율 (예: 0.032 = +3.2%)
  currency: string;
  openPrice?: number;
  highPrice?: number;
  lowPrice?: number;
  volume?: number;
}
