//! Publish behaviour against the fake transport: the happy paths, and every
//! refusal that has to happen before a single byte leaves the process.

use xpst_provider_bluesky::fake::FakeTransport;
use xpst_provider_bluesky::{
    BlueskyCredentials, BlueskyProvider, FailureCode, ImageInput, PublishOutcome, PublishedKind,
    RefusalCode, TransportError, MAX_GRAPHEMES, MAX_IMAGES_PER_POST, MAX_IMAGE_BYTES,
    MAX_TEXT_BYTES,
};

fn provider(fake: FakeTransport) -> BlueskyProvider<FakeTransport> {
    BlueskyProvider::new(
        fake,
        BlueskyCredentials::new("bot.example", "app-password-1234"),
    )
}

fn png(filename: &str, len: usize) -> ImageInput {
    ImageInput::new(filename, vec![0u8; len]).with_alt("alt text")
}

/// A family emoji: one grapheme, 25 UTF-8 bytes.
fn family_emoji() -> &'static str {
    "\u{1F468}\u{200D}\u{1F469}\u{200D}\u{1F467}\u{200D}\u{1F466}"
}

#[tokio::test]
async fn text_post_is_published_with_a_record_uri() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let outcome = provider.publish_text("hello from xPST").await;

    let PublishOutcome::Published(post) = outcome else {
        panic!("expected a published post, got {outcome:?}");
    };
    // The URI is the one the request itself named: the record key is derived from
    // the content, so the adapter knows where the record will land before the PDS
    // answers, and a lost answer is still reconcilable.
    assert!(post
        .uri
        .starts_with("at://did:plc:xpstfakeaccount/app.bsky.feed.post/"));
    let rkey = post.uri.rsplit('/').next().unwrap_or_default();
    assert!(
        xpst_provider_bluesky::is_tid(rkey),
        "the record key must satisfy the lexicon's `tid` key type: {rkey}"
    );
    let expected_url = format!("https://bsky.app/profile/xpst-fake.example/post/{rkey}");
    assert_eq!(post.url.as_deref(), Some(expected_url.as_str()));
    assert_eq!(post.kind, PublishedKind::Text);

    let drafts = provider.transport().drafts();
    assert_eq!(drafts.len(), 1);
    assert_eq!(drafts[0].text, "hello from xPST");
    assert_eq!(
        drafts[0].rkey, rkey,
        "the transport must be told which record key to write"
    );
    assert!(drafts[0].images.is_empty());
    // `createdAt` has to be a lexicon timestamp, not a local-time string.
    assert!(
        drafts[0].created_at.ends_with('Z'),
        "createdAt was {:?}",
        drafts[0].created_at
    );
    assert!(drafts[0].created_at.parse::<jiff::Timestamp>().is_ok());
}

#[tokio::test]
async fn the_session_is_created_once_for_repeated_posts() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    provider.publish_text("first").await;
    provider.publish_text("second").await;

    let sessions = provider
        .transport()
        .calls()
        .into_iter()
        .filter(|call| call.nsid() == "com.atproto.server.createSession")
        .count();
    assert_eq!(
        sessions, 1,
        "createSession is the rate-limited call and must be cached"
    );
}

#[tokio::test]
async fn links_become_facets_with_utf8_byte_offsets() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    provider
        .publish_text("héllo https://example.com/path.")
        .await;

    let drafts = provider.transport().drafts();
    let facets = &drafts[0].facets;
    assert_eq!(facets.len(), 1);
    assert_eq!(facets[0].uri, "https://example.com/path");
    // "héllo " is 7 bytes, not 6: the facet index must be bytes.
    assert_eq!(facets[0].byte_start, 7);
    assert_eq!(facets[0].byte_end, 7 + "https://example.com/path".len());
}

#[tokio::test]
async fn an_image_post_uploads_one_blob_and_embeds_it() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let outcome = provider
        .publish_images(&[png("photo.png", 1024)], "a caption")
        .await;

    let PublishOutcome::Published(post) = outcome else {
        panic!("expected a published post, got {outcome:?}");
    };
    assert_eq!(post.kind, PublishedKind::Image { count: 1 });

    let uploads = provider.transport().uploads();
    assert_eq!(uploads, vec![("image/png".to_owned(), 1024)]);

    let drafts = provider.transport().drafts();
    assert_eq!(drafts[0].text, "a caption");
    assert_eq!(drafts[0].images.len(), 1);
    assert_eq!(drafts[0].images[0].alt, "alt text");
    assert_eq!(drafts[0].images[0].blob.mime, "image/png");
    assert_eq!(drafts[0].images[0].blob.size, 1024);
}

#[tokio::test]
async fn four_images_are_one_native_multi_image_post() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let images: Vec<ImageInput> = (0..MAX_IMAGES_PER_POST)
        .map(|index| png(&format!("p{index}.jpg"), 16))
        .collect();
    let outcome = provider.publish_images(&images, "").await;

    let PublishOutcome::Published(post) = outcome else {
        panic!("expected a published post, got {outcome:?}");
    };
    assert_eq!(
        post.kind,
        PublishedKind::Image {
            count: MAX_IMAGES_PER_POST
        }
    );
    assert_eq!(provider.transport().uploads().len(), MAX_IMAGES_PER_POST);
    assert_eq!(
        provider.transport().drafts()[0].images.len(),
        MAX_IMAGES_PER_POST
    );
    // An empty caption is legitimate when the post carries an embed.
    assert_eq!(provider.transport().drafts()[0].text, "");
}

#[tokio::test]
async fn five_images_are_refused_with_both_numbers_and_nothing_is_uploaded() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let images: Vec<ImageInput> = (0..MAX_IMAGES_PER_POST + 1)
        .map(|index| png(&format!("p{index}.png"), 16))
        .collect();
    let outcome = provider.publish_images(&images, "five").await;

    let PublishOutcome::Refused(refusal) = outcome else {
        panic!("expected a refusal, got {outcome:?}");
    };
    assert_eq!(refusal.code, RefusalCode::TooManyImages);
    assert_eq!(refusal.measured, Some(5));
    assert_eq!(refusal.limit, Some(MAX_IMAGES_PER_POST));
    assert!(refusal.message.contains("5 images"), "{}", refusal.message);
    assert!(
        refusal.message.contains("at most 4 images"),
        "{}",
        refusal.message
    );
    assert_eq!(
        provider.transport().call_count(),
        0,
        "a refusal must not reach the PDS"
    );
}

#[tokio::test]
async fn over_300_graphemes_is_refused_with_the_measured_count() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let text = "a".repeat(MAX_GRAPHEMES + 1);
    let outcome = provider.publish_text(&text).await;

    let PublishOutcome::Refused(refusal) = outcome else {
        panic!("expected a refusal, got {outcome:?}");
    };
    assert_eq!(refusal.code, RefusalCode::TextTooManyGraphemes);
    assert_eq!(refusal.measured, Some(301));
    assert_eq!(refusal.limit, Some(300));
    assert!(
        refusal.message.contains("301 graphemes"),
        "{}",
        refusal.message
    );
    assert!(
        refusal.message.contains("at most 300 graphemes"),
        "{}",
        refusal.message
    );
    assert!(
        refusal.message.contains("remove at least 1"),
        "{}",
        refusal.message
    );
    assert_eq!(provider.transport().call_count(), 0);
}

#[tokio::test]
async fn over_3000_bytes_is_refused_even_when_the_grapheme_count_fits() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    // 150 graphemes, 3,750 bytes: the grapheme cap is not the one that trips.
    let text = family_emoji().repeat(150);
    let outcome = provider.publish_text(&text).await;

    let PublishOutcome::Refused(refusal) = outcome else {
        panic!("expected a refusal, got {outcome:?}");
    };
    assert_eq!(refusal.code, RefusalCode::TextTooManyBytes);
    assert_eq!(refusal.measured, Some(3_750));
    assert_eq!(refusal.limit, Some(MAX_TEXT_BYTES));
    assert!(
        refusal.message.contains("3,750 bytes"),
        "{}",
        refusal.message
    );
    assert!(
        refusal.message.contains("at most 3,000 bytes"),
        "{}",
        refusal.message
    );
    assert_eq!(provider.transport().call_count(), 0);
}

#[tokio::test]
async fn a_post_exactly_at_the_text_limit_is_published() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let text = "a".repeat(MAX_GRAPHEMES);
    let outcome = provider.publish_text(&text).await;

    assert!(
        outcome.is_published(),
        "the boundary must be inclusive, got {outcome:?}"
    );
}

#[tokio::test]
async fn empty_text_is_refused_instead_of_writing_a_blank_record() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let outcome = provider.publish_text("   ").await;

    let PublishOutcome::Refused(refusal) = outcome else {
        panic!("expected a refusal, got {outcome:?}");
    };
    assert_eq!(refusal.code, RefusalCode::EmptyText);
    assert_eq!(provider.transport().call_count(), 0);
}

#[tokio::test]
async fn an_oversized_image_is_refused_with_its_size_and_the_cap() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let oversized = ImageInput::new("huge.png", vec![0u8; MAX_IMAGE_BYTES + 1]);
    let outcome = provider.publish_images(&[oversized], "too big").await;

    let PublishOutcome::Refused(refusal) = outcome else {
        panic!("expected a refusal, got {outcome:?}");
    };
    assert_eq!(refusal.code, RefusalCode::ImageTooLarge);
    assert_eq!(refusal.measured, Some(MAX_IMAGE_BYTES + 1));
    assert_eq!(refusal.limit, Some(MAX_IMAGE_BYTES));
    assert!(
        refusal.message.contains("2,000,001 bytes"),
        "{}",
        refusal.message
    );
    assert!(
        refusal.message.contains("at most 2,000,000 bytes"),
        "{}",
        refusal.message
    );
    assert_eq!(provider.transport().call_count(), 0);
}

#[tokio::test]
async fn an_image_exactly_at_the_cap_is_uploaded() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let at_cap = ImageInput::new("big.png", vec![0u8; MAX_IMAGE_BYTES]);
    let outcome = provider.publish_images(&[at_cap], "at the cap").await;

    assert!(
        outcome.is_published(),
        "the boundary must be inclusive, got {outcome:?}"
    );
    assert_eq!(
        provider.transport().uploads(),
        vec![("image/png".to_owned(), MAX_IMAGE_BYTES)]
    );
}

#[tokio::test]
async fn an_unsupported_image_type_is_refused_by_name() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let outcome = provider
        .publish_images(&[png("scan.tiff", 32)], "nope")
        .await;

    let PublishOutcome::Refused(refusal) = outcome else {
        panic!("expected a refusal, got {outcome:?}");
    };
    assert_eq!(refusal.code, RefusalCode::ImageUnsupportedType);
    assert!(refusal.message.contains("tiff"), "{}", refusal.message);
    assert!(refusal.message.contains("scan.tiff"), "{}", refusal.message);
    assert_eq!(provider.transport().call_count(), 0);
}

#[tokio::test]
async fn an_over_limit_caption_refuses_before_any_upload() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let images: Vec<ImageInput> = (0..2)
        .map(|index| png(&format!("p{index}.png"), 8))
        .collect();
    let caption = "a".repeat(MAX_GRAPHEMES + 5);
    let outcome = provider.publish_images(&images, &caption).await;

    let PublishOutcome::Refused(refusal) = outcome else {
        panic!("expected a refusal, got {outcome:?}");
    };
    assert_eq!(refusal.code, RefusalCode::TextTooManyGraphemes);
    // Validation happens before the first blob upload: no half-published post.
    assert_eq!(
        provider.transport().call_count(),
        0,
        "nothing may be uploaded for a refused post"
    );
}

#[tokio::test]
async fn a_rejected_app_password_surfaces_as_needs_reauth() {
    let fake = FakeTransport::new();
    fake.fail_session(TransportError::CredentialsRejected {
        detail: "InvalidIdentifier: unable to resolve handle".to_owned(),
    });
    let provider = provider(fake);

    let outcome = provider.publish_text("hello").await;

    let PublishOutcome::NeedsReauth { detail } = outcome else {
        panic!("expected needs_reauth, got {outcome:?}");
    };
    assert!(detail.contains("BLUESKY_AUTH_FAILED"), "{detail}");
    assert!(detail.contains("unable to resolve handle"), "{detail}");
    // A rejected credential must not be retried blindly.
    assert_eq!(provider.transport().call_count(), 1);
}

#[tokio::test]
async fn a_session_that_expires_mid_post_surfaces_as_needs_reauth_and_is_dropped() {
    let fake = FakeTransport::new();
    fake.fail_create(TransportError::Unauthorized {
        detail: "ExpiredToken: token expired".to_owned(),
    });
    let provider = provider(fake);

    let outcome = provider.publish_text("hello").await;
    let PublishOutcome::NeedsReauth { detail } = outcome else {
        panic!("expected needs_reauth, got {outcome:?}");
    };
    assert!(detail.contains("ExpiredToken"), "{detail}");

    // The dead session is forgotten, so the next attempt logs in again.
    provider.transport().clear_failures();
    let sessions_before = provider
        .transport()
        .calls()
        .into_iter()
        .filter(|call| call.nsid() == "com.atproto.server.createSession")
        .count();
    let _ = provider.publish_text("hello again").await;
    let sessions_after = provider
        .transport()
        .calls()
        .into_iter()
        .filter(|call| call.nsid() == "com.atproto.server.createSession")
        .count();
    assert_eq!(sessions_after, sessions_before + 1);
}

#[tokio::test]
async fn a_throttled_pds_is_reported_as_retryable() {
    let fake = FakeTransport::new();
    fake.fail_create(TransportError::RateLimited {
        retry_after_s: Some(30),
        detail: "RateLimitExceeded: too many requests".to_owned(),
    });
    let provider = provider(fake);

    let outcome = provider.publish_text("hello").await;

    let PublishOutcome::Failed {
        code,
        retryable,
        detail,
    } = outcome
    else {
        panic!("expected a failure, got {outcome:?}");
    };
    assert_eq!(code, FailureCode::RateLimited);
    assert!(retryable);
    assert!(detail.contains("BLUESKY_POST_FAILED"), "{detail}");
    assert!(
        detail.contains("30"),
        "the PDS's own window must survive into the message: {detail}"
    );
}

#[tokio::test]
async fn video_is_routed_to_the_video_service_and_never_faked() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let refusal = provider.refuse_video();

    assert_eq!(refusal.code, RefusalCode::VideoNeedsVideoService);
    assert!(
        refusal.message.contains("https://video.bsky.app"),
        "{}",
        refusal.message
    );
    assert!(
        refusal.message.contains("app.bsky.feed.post"),
        "{}",
        refusal.message
    );
    assert_eq!(provider.transport().call_count(), 0);
    assert_eq!(
        provider.video_route(),
        xpst_provider_bluesky::VideoRoute::VideoService {
            service: "https://video.bsky.app"
        }
    );
}

#[tokio::test]
async fn publishing_a_video_refuses_and_touches_nothing() {
    let fake = FakeTransport::new();
    let provider = provider(fake);

    let outcome = provider.publish_video("a clip").await;

    let PublishOutcome::Refused(refusal) = outcome else {
        panic!("expected a refusal, got {outcome:?}");
    };
    assert_eq!(refusal.code, RefusalCode::VideoNeedsVideoService);
    assert!(
        refusal.message.contains("https://video.bsky.app"),
        "{}",
        refusal.message
    );
    assert_eq!(provider.transport().call_count(), 0);
}

#[test]
fn the_capability_declaration_matches_the_refusals() {
    let capabilities = xpst_provider_bluesky::BlueskyCapabilities::REFERENCE;

    assert_eq!(capabilities.text.max_graphemes, MAX_GRAPHEMES);
    assert_eq!(capabilities.text.max_bytes, MAX_TEXT_BYTES);
    assert_eq!(capabilities.images.max_images_per_post, MAX_IMAGES_PER_POST);
    assert_eq!(capabilities.images.max_bytes_per_image, MAX_IMAGE_BYTES);
    assert!(capabilities.carousel);
    assert!(capabilities.delete);
    assert!(
        !capabilities.soft_delete,
        "AT Protocol has no reversible unpublish"
    );

    // The refusal's limit fields are the capability values, not a second copy.
    let refusal = xpst_provider_bluesky::capabilities::check_image_count(9).unwrap_err();
    assert_eq!(refusal.limit, Some(capabilities.images.max_images_per_post));
    let refusal =
        xpst_provider_bluesky::capabilities::check_image("x.png", &vec![0; MAX_IMAGE_BYTES + 1])
            .unwrap_err();
    assert_eq!(refusal.limit, Some(capabilities.images.max_bytes_per_image));
}
