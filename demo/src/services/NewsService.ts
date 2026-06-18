import type { NewsItem } from "../domain/news/NewsItem";
import type { NewsRepository } from "../infrastructure/storage/NewsRepository";

export class NewsService {
  constructor(private readonly repository: NewsRepository) {}

  async getTurnNews(params: {
    turnIndex: number;
    selectedAssetId?: string | null;
    limit?: number;
  }): Promise<NewsItem[]> {
    if (params.turnIndex <= 0) {
      return [];
    }

    return this.repository.findByTurn({
      turnIndex: params.turnIndex,
      assetId: params.selectedAssetId ?? null,
      includeGlobal: true,
      limit: params.limit ?? 5,
    });
  }
}