import { NewsService } from "../../services/NewsService";
import { SupabaseNewsRepository } from "../../infrastructure/storage/SupabaseNewsRepository";

const newsRepository = new SupabaseNewsRepository();

export const newsService = new NewsService(newsRepository);