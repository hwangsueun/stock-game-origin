import { NewsSentiment } from "./NewsSentiment";

export class NewsItem {
  readonly id: string;
  readonly turnIndex: number;
  readonly gameDate: string;
  readonly assetId: string | null;
  readonly headline: string;
  readonly body: string;
  readonly assetClass: string | null;
  readonly sentiment: NewsSentiment;
  readonly importance: number;
  readonly newsOrder: number | null;

  constructor(params: {
    id: string;
    turnIndex: number;
    gameDate: string;
    assetId?: string | null;
    headline: string;
    body: string;
    assetClass?: string | null;
    sentiment?: NewsSentiment;
    importance?: number;
    newsOrder?: number | null;
  }) {
    if (params.turnIndex <= 0) {
      throw new Error("NewsItem: turnIndex must be positive");
    }

    if (!params.headline.trim()) {
      throw new Error("NewsItem: headline is empty");
    }

    if (!params.body.trim()) {
      throw new Error("NewsItem: body is empty");
    }

    this.id = params.id;
    this.turnIndex = params.turnIndex;
    this.gameDate = params.gameDate;
    this.assetId = params.assetId ?? null;
    this.headline = params.headline;
    this.body = params.body;
    this.assetClass = params.assetClass ?? null;
    this.sentiment = params.sentiment ?? NewsSentiment.NEUTRAL;
    this.importance = params.importance ?? 1;
    this.newsOrder = params.newsOrder ?? null;
  }

  isGlobalNews(): boolean {
    return this.assetId === null;
  }
}