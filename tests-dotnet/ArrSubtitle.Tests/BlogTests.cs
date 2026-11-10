using ArrSubtitle.Core.Blogs;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using Xunit;

namespace ArrSubtitle.Tests;

public sealed class BlogTests
{
    [Theory]
    [InlineData("https://m.blog.naver.com/author/123", BlogPlatform.Naver, "author")]
    [InlineData("https://blog.naver.com/PostView.naver?blogId=author&logNo=123", BlogPlatform.Naver, "author")]
    [InlineData("https://author.blogspot.com/2026/11/post.html", BlogPlatform.Blogspot, "author.blogspot.com")]
    [InlineData("https://author.tistory.com/123", BlogPlatform.Tistory, "author.tistory.com")]
    public void DetectsKnownBlogFromPostUrl(string url, BlogPlatform platform, string id)
    {
        var blog = BlogDetector.Known(new(url));
        Assert.Equal(platform, blog!.Platform);
        Assert.Equal(id, blog.Id);
    }

    [Fact]
    public void NaverScopeRejectsOtherBlogId()
    {
        var blog = BlogDetector.Known(new("https://blog.naver.com/author"))!;
        Assert.False(BlogScope.Contains(blog, new("https://blog.naver.com/other/123")));
        Assert.False(BlogScope.Contains(blog, new("https://blog.naver.com/PostView.naver?blogId=other")));
        Assert.True(BlogScope.Contains(blog, new("https://m.blog.naver.com/author/123")));
    }

    [Fact]
    public async Task RegistrationDoesNotEnumeratePosts()
    {
        var handler = new ReplayHandler(_ => throw new Exception("No request expected"));
        using var session = new RequestSession(handler);
        await new BlogDetector(session).DetectAsync(new("https://author.blogspot.com/"), default);
        Assert.Empty(handler.Calls);
    }

    [Fact]
    public async Task ReadsOlderBlogspotFeedPage()
    {
        var handler = new ReplayHandler(r => ReplayHandler.Text(r.RequestUri!.Query.Contains("start-index")
            ? """{"feed":{"entry":[{"title":{"$t":"테스트 1화 자막"},"content":{"$t":"<a href='https://example.com/01.smi'>01.smi</a>"},"link":[{"rel":"alternate","href":"https://author.blogspot.com/1.html"}]}],"link":[]}}"""
            : """{"feed":{"link":[{"rel":"next","href":"https://author.blogspot.com/feeds/posts/default?alt=json&start-index=51"}]}}"""));
        using var session = new RequestSession(handler);
        var explorer = new BlogExplorer(session);
        var posts = new List<PostCandidate>();
        await foreach (var post in explorer.ExploreAsync(new("https://author.blogspot.com/", BlogPlatform.Blogspot, "author.blogspot.com"), new(1, "테스트", "Test", ["테스트"], "fixture"), default)) posts.Add(post);
        Assert.Single(posts);
        Assert.True(explorer.Complete);
        Assert.Equal(2, handler.Calls.Count);
    }

    [Fact]
    public async Task FollowsNaverMainFrameWithinBlogScope()
    {
        var handler = new ReplayHandler(r => ReplayHandler.Text(r.RequestUri!.AbsolutePath == "/author/1" ? "<iframe id='mainFrame' src='/PostView.naver?blogId=author&logNo=1'></iframe>" : "<div class='post-body'>unrelated sidebar</div><h1>테스트 1화</h1><div class='se-main-container'><a href='https://example.com/01.smi'>01.smi</a></div>", "text/html"));
        using var session = new RequestSession(handler);
        var post = await new BlogExplorer(session).ReadPostAsync("https://blog.naver.com/author/1", default);
        Assert.Contains("01.smi", post.Html);
        Assert.Equal(2, handler.Calls.Count);
    }

    [Fact]
    public async Task GenericSitemapFindsOlderPost()
    {
        var handler = new ReplayHandler(r => r.RequestUri!.AbsolutePath switch
        {
            "/" => ReplayHandler.Text("<main><p>Blog</p></main>", "text/html"),
            "/rss" => new(System.Net.HttpStatusCode.NotFound),
            "/sitemap.xml" => ReplayHandler.Text("<urlset><url><loc>https://example.com/older</loc></url></urlset>", "application/xml"),
            _ => ReplayHandler.Text("<meta property='og:title' content='테스트 1화 자막'><article><a href='/01.smi'>01.smi</a></article>", "text/html")
        });
        using var session = new RequestSession(handler);
        var explorer = new BlogExplorer(session); var posts = new List<PostCandidate>();
        await foreach (var post in explorer.ExploreAsync(new("https://example.com/", BlogPlatform.Generic, "example.com:/"), new(1, "테스트", "Test", ["테스트"], "fixture"), default)) posts.Add(post);
        Assert.Single(posts, p => p.Title == "테스트 1화 자막");
        Assert.True(explorer.Complete);
    }

    [Fact]
    public async Task TistoryLegacyBodyIsRecognizedDuringFallback()
    {
        var handler = new ReplayHandler(r => r.RequestUri!.AbsolutePath switch
        {
            "/" => ReplayHandler.Text("<main><a href='/1'>테스트 1화 자막</a></main>", "text/html"),
            "/rss" => new(System.Net.HttpStatusCode.NotFound),
            "/1" => ReplayHandler.Text("<div class='tit_post'>Blog name</div><meta property='og:title' content='테스트 1화 자막'><div class='tt_article_useless_p_margin'><a href='https://files.example.com/01.smi'>01.smi</a></div>", "text/html"),
            _ => ReplayHandler.Text("<main>No results</main>", "text/html")
        });
        using var session = new RequestSession(handler);
        var explorer = new BlogExplorer(session); var found = false;
        await foreach (var post in explorer.ExploreAsync(new("https://author.tistory.com/", BlogPlatform.Tistory, "author.tistory.com"), new(1, "테스트", "Test", ["테스트"], "fixture"), default)) if (post.Title == "테스트 1화 자막") found = true;
        Assert.True(found);
    }
}
