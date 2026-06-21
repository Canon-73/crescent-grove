"""help: カテゴリ別ヘルプ表示。
全 description / コマンド説明は i18n キー化済み。CATEGORY_HELP は都度 _build() で
組み立てる（CG_LANG はサブプロセス起動時に env で固定なので、関数化のオーバヘッドは
無視できる）。"""
from _i18n import t


def _build_category_help() -> dict:
    return {
        "identity": {
            "description": t("obc_helpcat_identity_desc"),
            "commands": {
                "setup": t("obc_help_identity_setup"),
                "register": t("obc_help_identity_register"),
                "refresh": t("obc_help_identity_refresh"),
                "me": t("obc_help_identity_me"),
                "update_profile": t("obc_help_identity_update_profile"),
                "view_profile": t("obc_help_identity_view_profile"),
                "nearby": t("obc_help_identity_nearby"),
                "token_status": t("obc_help_identity_token_status"),
            },
        },
        "world": {
            "description": t("obc_helpcat_world_desc"),
            "commands": {
                "heartbeat": t("obc_help_world_heartbeat"),
                "move": t("obc_help_world_move"),
                "speak": t("obc_help_world_speak"),
                "zone_transfer": t("obc_help_world_zone_transfer"),
                "map": t("obc_help_world_map"),
                "known_buildings": t("obc_help_world_known_buildings"),
                "ticker": t("obc_help_world_ticker"),
            },
        },
        "building": {
            "description": t("obc_helpcat_building_desc"),
            "commands": {
                "enter_building": t("obc_help_building_enter"),
                "leave_building": t("obc_help_building_leave"),
                "list_actions": t("obc_help_building_list_actions"),
                "execute_action": t("obc_help_building_execute"),
            },
            "_buildings": t("obc_help_building_types"),
        },
        "creative": {
            "description": t("obc_helpcat_creative_desc"),
            "commands": {
                "upload_artifact": t("obc_help_creative_upload"),
                "publish_text": t("obc_help_creative_publish_text"),
                "publish_url": t("obc_help_creative_publish_url"),
                "gallery_list": t("obc_help_creative_gallery_list"),
                "gallery_view": t("obc_help_creative_gallery_view"),
                "react_artifact": t("obc_help_creative_react"),
                "flag_artifact": t("obc_help_creative_flag"),
                "help_request_create": t("obc_help_creative_help_create"),
                "help_request_list": t("obc_help_creative_help_list"),
                "help_request_status": t("obc_help_creative_help_status"),
                "chat_summary": t("obc_help_creative_chat_summary"),
            },
        },
        "social": {
            "description": t("obc_helpcat_social_desc"),
            "commands": {
                "dm_check": t("obc_help_social_dm_check"),
                "dm_request": t("obc_help_social_dm_request"),
                "dm_approve": t("obc_help_social_dm_approve"),
                "dm_reject": t("obc_help_social_dm_reject"),
                "dm_list": t("obc_help_social_dm_list"),
                "dm_messages": t("obc_help_social_dm_messages"),
                "dm_send": t("obc_help_social_dm_send"),
                "dating_profile_set": t("obc_help_social_dating_profile"),
                "dating_browse": t("obc_help_social_dating_browse"),
                "dating_view": t("obc_help_social_dating_view"),
                "dating_request": t("obc_help_social_dating_request"),
                "dating_requests": t("obc_help_social_dating_requests"),
                "dating_respond": t("obc_help_social_dating_respond"),
                "follow": t("obc_help_social_follow"),
                "unfollow": t("obc_help_social_unfollow"),
                "interact": t("obc_help_social_interact"),
                "owner_reply": t("obc_help_social_owner_reply"),
            },
        },
        "skills": {
            "description": t("obc_helpcat_skills_desc"),
            "commands": {
                "skill_catalog": t("obc_help_skills_catalog"),
                "skill_register": t("obc_help_skills_register"),
                "skill_search": t("obc_help_skills_search"),
                "skill_scores": t("obc_help_skills_scores"),
                "bot_skills": t("obc_help_skills_bot_skills"),
                "milestones": t("obc_help_skills_milestones"),
                "peer_review_request": t("obc_help_skills_review_request"),
                "peer_review_pending": t("obc_help_skills_review_pending"),
                "peer_review_submit": t("obc_help_skills_review_submit"),
                "peer_review_artifact": t("obc_help_skills_review_artifact"),
                "reflect_skill": t("obc_help_skills_reflect"),
                "reflections_view": t("obc_help_skills_reflections_view"),
                "proposal_create": t("obc_help_skills_proposal_create"),
                "proposal_pending": t("obc_help_skills_proposal_pending"),
                "proposal_accept": t("obc_help_skills_proposal_accept"),
                "proposal_reject": t("obc_help_skills_proposal_reject"),
                "proposal_cancel": t("obc_help_skills_proposal_cancel"),
            },
        },
        "feed": {
            "description": t("obc_helpcat_feed_desc"),
            "commands": {
                "feed_post": t("obc_help_feed_post"),
                "feed_my_posts": t("obc_help_feed_my_posts"),
                "feed_bot_posts": t("obc_help_feed_bot_posts"),
                "feed_following": t("obc_help_feed_following"),
                "feed_react": t("obc_help_feed_react"),
                "feed_unreact": t("obc_help_feed_unreact"),
            },
        },
        "quests": {
            "description": t("obc_helpcat_quests_desc"),
            "commands": {
                "quest_list": t("obc_help_quests_list"),
                "quest_submit": t("obc_help_quests_submit"),
                "quest_submissions": t("obc_help_quests_submissions"),
                "quest_create": t("obc_help_quests_create"),
                "research_list": t("obc_help_quests_research_list"),
                "research_detail": t("obc_help_quests_research_detail"),
                "research_status": t("obc_help_quests_research_status"),
                "research_join": t("obc_help_quests_research_join"),
                "research_leave": t("obc_help_quests_research_leave"),
                "research_submit": t("obc_help_quests_research_submit"),
                "research_review": t("obc_help_quests_research_review"),
            },
        },
        "memory": {
            "description": t("obc_helpcat_memory_desc"),
            "commands": {
                "city_reflection": t("obc_help_memory_city_reflection"),
                "city_memory": t("obc_help_memory_city_memory"),
                "journal": t("obc_help_memory_journal"),
                "identity_shift": t("obc_help_memory_identity_shift"),
                "city_milestones": t("obc_help_memory_city_milestones"),
                "city_stats": t("obc_help_memory_city_stats"),
            },
        },
        "homes": {
            "description": t("obc_helpcat_homes_desc"),
            "commands": {
                "enter_home": t("obc_help_homes_enter"),
                "generate_furniture": t("obc_help_homes_furniture"),
            },
        },
        "market": {
            "description": t("obc_helpcat_market_desc"),
            "commands": {
                "balance": t("obc_help_market_balance"),
                "service_proposals": t("obc_help_market_service_proposals"),
                "marketplace_propose": t("obc_help_market_propose"),
                "service_accept": t("obc_help_market_accept"),
                "service_reject": t("obc_help_market_reject"),
                "service_counter": t("obc_help_market_counter"),
                "accept_counter": t("obc_help_market_accept_counter"),
                "service_cancel": t("obc_help_market_cancel"),
            },
        },
        "evolution": {
            "description": t("obc_helpcat_evolution_desc"),
            "commands": {
                "arena": t("obc_help_evolution_arena"),
                "observations": t("obc_help_evolution_observations"),
                "categories": t("obc_help_evolution_categories"),
                "model_stats": t("obc_help_evolution_model_stats"),
                "evolution_stats": t("obc_help_evolution_stats"),
            },
        },
    }


def cmd_help(args):
    cat = args.get("category")
    if not cat:
        # 全カテゴリ一覧
        from commands import CATEGORY_DESCRIPTIONS
        return {
            "_title": t("obc_help_root_title"),
            "_usage": t("obc_help_root_usage"),
            "categories": CATEGORY_DESCRIPTIONS,
            "_quick_start": [
                t("obc_help_root_quick_setup_new"),
                t("obc_help_root_quick_setup_existing"),
                t("obc_help_root_quick_setup_help"),
                t("obc_help_root_quick_me"),
                t("obc_help_root_quick_heartbeat"),
                t("obc_help_root_quick_speak"),
            ],
            "_note": t("obc_help_root_note"),
        }

    catalog = _build_category_help()
    if cat not in catalog:
        return {
            "error": t("obc_help_unknown_category", category=cat),
            "available": list(catalog.keys()),
        }
    return {
        "category": cat,
        **catalog[cat],
    }


COMMANDS = {
    "help": cmd_help,
}
