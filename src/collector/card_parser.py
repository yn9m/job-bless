import logging
import re
from typing import List, Tuple, Optional, Dict, Any
from playwright.async_api import Page, ElementHandle

from src.db.models import VacancyCard

logger = logging.getLogger(__name__)


class HHSelectors:
    VACANCY_CARD = '[data-qa="vacancy-serp__vacancy"]'
    TITLE_LINK = '[data-qa="serp-item__title"]'
    COMPANY_NAME = '[data-qa="vacancy-serp__vacancy-employer"]'
    SALARY = '[data-qa="vacancy-serp__vacancy-compensation"]'
    CITY = '[data-qa="vacancy-serp__vacancy-address"]'
    WORK_FORMAT = '[data-qa="vacancy-serp__vacancy-work-format"]'
    EXPERIENCE = '[data-qa="vacancy-serp__vacancy-experience"]'
    SNIPPET = '[data-qa="vacancy-serp__vacancy_snippet_responsibility"]'
    TAGS = '[data-qa="vacancy-serp__vacancy_snippet_requirement"]'


class VacancyCardParser:
    """
    Parses vacancy card elements from Playwright Page DOM into VacancyCard instances.
    """

    async def parse_cards_from_page(
        self, page: Page, page_number: int = 1, search_url: str = ""
    ) -> List[Tuple[VacancyCard, Optional[str]]]:
        cards: List[Tuple[VacancyCard, Optional[str]]] = []
        try:
            elements = await page.query_selector_all(HHSelectors.VACANCY_CARD)
            logger.info(f"Found {len(elements)} vacancy card elements on page #{page_number}")

            for pos, elem in enumerate(elements, start=1):
                try:
                    card = await self.parse_single_card(elem, page_number=page_number, position=pos, search_url=search_url)
                    if card and card.external_id:
                        cards.append((card, None))
                except Exception as e:
                    logger.warning(f"Error parsing card #{pos} on page {page_number}: {e}")

        except Exception as e:
            logger.error(f"Error querying vacancy card elements: {e}")

        return cards

    async def parse_single_card(
        self, elem: ElementHandle, page_number: int = 1, position: int = 1, search_url: str = ""
    ) -> Optional[VacancyCard]:
        title_elem = await elem.query_selector(HHSelectors.TITLE_LINK)
        if not title_elem:
            return None

        url = await title_elem.get_attribute("href") or ""
        title = (await title_elem.inner_text()).strip()

        # Extract external ID from URL (e.g. /vacancy/12345678)
        external_id = ""
        match = re.search(r"/vacancy/(\d+)", url)
        if match:
            external_id = match.group(1)
        else:
            external_id = url.split("?")[0].rstrip("/").split("/")[-1]

        # Clean canonical URL
        canonical_url = f"https://hh.ru/vacancy/{external_id}" if external_id.isdigit() else url

        # Company
        company_name = ""
        company_elem = await elem.query_selector(HHSelectors.COMPANY_NAME)
        if company_elem:
            company_name = (await company_elem.inner_text()).strip()

        # Salary
        salary_text = ""
        salary_elem = await elem.query_selector(HHSelectors.SALARY)
        if salary_elem:
            salary_text = (await salary_elem.inner_text()).strip()

        # City
        city = ""
        city_elem = await elem.query_selector(HHSelectors.CITY)
        if city_elem:
            city = (await city_elem.inner_text()).strip()

        # Work format & Experience
        work_format = ""
        work_elem = await elem.query_selector(HHSelectors.WORK_FORMAT)
        if work_elem:
            work_format = (await work_elem.inner_text()).strip()

        experience = ""
        exp_elem = await elem.query_selector(HHSelectors.EXPERIENCE)
        if exp_elem:
            experience = (await exp_elem.inner_text()).strip()

        snippet = ""
        snip_elem = await elem.query_selector(HHSelectors.SNIPPET)
        if snip_elem:
            snippet = (await snip_elem.inner_text()).strip()

        return VacancyCard(
            source="hh",
            external_id=external_id,
            url=canonical_url,
            title=title,
            company_name=company_name,
            salary_text=salary_text,
            city=city,
            work_format=work_format,
            experience=experience,
            snippet=snippet,
            page_number=page_number,
            position_on_page=position,
            search_url=search_url,
        )
