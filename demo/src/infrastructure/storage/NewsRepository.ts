import type { NewsItem } from "../../domain/news/NewsItem";

export type NewsQuery = {
  turnIndex: number;
  assetId?: string | null;
  includeGlobal?: boolean;
  limit?: number;
};

export interface NewsRepository {
  findByTurn(query: NewsQuery): Promise<NewsItem[]>;
}